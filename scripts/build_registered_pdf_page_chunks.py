#!/usr/bin/env python3
"""Bounded PDF-first chunks for explicitly selected Paper KB registered sources.

Exact registered SHA + PDF page witnesses; no GROBID or embeddings. Fail closed
on source drift, prior derivatives, title mismatch, bad pages or QA. Each corpus
is staged then promoted with rollback if strict corpus doctor fails.
"""
from __future__ import annotations
import argparse,hashlib,json,re,shutil,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pipeline.writers.chunk_set_writer import write_chunk_set_artifact
from pipeline.projections.catalog_records import export_catalog_records
from pipeline.projections.review_records import export_review_records

PARSER='poppler-layout-page-bounded-lossless-normalized-v1'
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for buf in iter(lambda:f.read(1<<20),b''):h.update(buf)
 return h.hexdigest()
def norm(text):
 return ' '.join(text.split())
def title_match(title,pages):
 from unicodedata import normalize
 def tok(s):
  s=normalize('NFKD',s.casefold())
  return [w for w in re.findall(r'[a-z0-9]+',s) if len(w)>3]
 a=tok(title);b=set(tok(' '.join(pages[:2])[:18000]))
 return round(sum(x in b for x in a)/len(a),3) if a else 0
def pdf_pages(source,max_pages):
 info=subprocess.run(['pdfinfo',str(source)],capture_output=True,text=True,timeout=22,check=True).stdout
 m=re.search(r'(?m)^Pages:\s+(\d+)\s*$',info)
 if not m:raise ValueError(f'missing pdfinfo Pages for {source.name}')
 count=int(m.group(1))
 if count<2 or count>max_pages:raise ValueError(f'page budget exceeded {source.name}: {count}')
 raw=subprocess.run(['pdftotext','-layout',str(source),'-'],capture_output=True,
                    text=True,timeout=170,check=True).stdout.split('\f')
 if len(raw)==count+1 and not raw[-1].strip():raw.pop()
 if len(raw)!=count:
  # A few legitimate PDFs contain literal form-feed bytes inside a physical
  # page (e.g. W4415254724 final reference page). A global split then makes
  # false "pages". Extract each pdfinfo-indexed physical page independently,
  # treating embedded FF as whitespace, never redefining physical page anchors.
  perpage=[]
  for physical in range(1,count+1):
   one=subprocess.run(['pdftotext','-layout','-f',str(physical),'-l',str(physical),
                       str(source),'-'],capture_output=True,text=True,
                      timeout=22,check=True).stdout
   perpage.append(one.replace('\f',' '))
  raw=perpage
 if len(raw)!=count:raise ValueError(f'PDF page extraction count mismatch {source.name}: {len(raw)} != {count}')
 return raw
def page_chunks(pages,uid,filename,source_sha,max_total=5000,max_chars=1600):
 """No overlap, word-boundary chunks; all normalized non-empty page text retained.

 Each chunk is anchored to original physical page and a normalized character span.
 No section semantics are inferred (header_path remains blank).
 """
 chunks=[]; totals={'pages_with_text':0,'pages_empty':0,'normalized_chars':0}
 for n,raw in enumerate(pages,1):
  clean=norm(raw)
  if not clean:
   totals['pages_empty']+=1
   continue
  totals['pages_with_text']+=1
  totals['normalized_chars']+=len(clean)
  spans=[m.span() for m in re.finditer(r'\S+',clean)]
  assert spans
  pos=0;start=None;end=None;segments=[]
  for a,b in spans:
   if b-a>max_chars:raise ValueError(f'long unsplittable token on PDF page {n}: {b-a}')
   if start is None:start=a;end=b;continue
   if b-start>max_chars:
    segments.append((start,end))
    start=a
   end=b
  if start is not None:segments.append((start,end))
  assert ' '.join(clean[a:b] for a,b in segments)==clean, f'page {n} normalized text coverage mismatch'
  for part_index,(a,b) in enumerate(segments):
   part=clean[a:b]
   key=hashlib.sha256(f'{source_sha}:{n}:{part_index}:{part}'.encode()).hexdigest()[:16]
   chunks.append({
    'chunk_id':f'{uid}_p{n:03d}_{part_index:03d}_{key}',
    'paper_uid':uid,'paper_id':uid,'document_id':uid,
    'chunk_index':len(chunks),'text':part,
    'source_file':filename,'source_ref':'sha256:'+source_sha,
    'metadata':{'page_start':n,'page_end':n,'source_sha256':source_sha,
                'page_char_start':a,'page_char_end':b,'parser':PARSER}})
   if len(chunks)>max_total:raise ValueError('chunk budget exceeded')
 if not chunks:raise ValueError('PDF has no text selectable via pdftotext; manual OCR gate')
 assert sum(c['char_len'] if 'char_len' in c else len(c['text']) for c in chunks)<=totals['normalized_chars']
 totals['chunk_count']=len(chunks)
 return chunks,totals
def plan(corpus_name,planfile,max_files,max_bytes):
 if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',corpus_name):raise ValueError('bad corpus')
 corpus=ROOT/'corpora'/corpus_name
 if corpus.is_symlink():raise ValueError('symlink corpus rejected')
 manifest=corpus/'source-manifest.json'
 data=json.loads(manifest.read_text())
 if data.get('corpus_id')!=corpus_name:raise ValueError('corpus mismatch')
 rows={r['registered_filename']:r for r in data['files']}
 sel=json.loads(planfile.read_text())
 assert sel['schema']=='rp019-governed-chunking-plan.v1'
 chosen=[r for r in sel['sources'] if r['source_corpus']==corpus_name]
 if not 1<=len(chosen)<=max_files:raise ValueError(f'file budget for {corpus_name} ({len(chosen)})')
 if len({r['openalex_id'] for r in chosen})!=len(chosen):raise ValueError('duplicate plan IDs')
 total=0
 for r in chosen:
  name=r['source_filename']
  if name not in rows or name!=r['openalex_id']+'.pdf':raise ValueError('source filename mismatch')
  f=rows[name]
  src=corpus/'pdfs'/name
  if not src.is_file() or src.is_symlink():raise ValueError('missing or symlink source')
  if src.stat().st_size!=f['size_bytes'] or sha(src)!=f['sha256'] or f['sha256']!=r['source_sha256']:
   raise ValueError('source version changed since research selection: '+name)
  total+=f['size_bytes']
  if total>max_bytes:raise ValueError('total PDF byte budget exceeded')
 for n in ['chunk_sets','catalog','review']:
  p=corpus/n
  if p.is_symlink():raise ValueError('symlink derivative dir rejected')
  if p.exists() and any(p.iterdir()):raise FileExistsError('existing derivatives, refuse overwrite: '+str(p))
 return corpus,chosen,{'corpus':corpus_name,'selected_count':len(chosen),
  'source_bytes':total,'source_manifest_sha256':sha(manifest),'selection_sha256':sha(planfile),
  'parser':PARSER,'max_files':max_files,'max_bytes':max_bytes}
def process(corpus,rows,preflight,max_pages,max_chunks):
 artifacts=[];total_chunks=0
 with tempfile.TemporaryDirectory(prefix='.private_page_chunk_stage_',dir=corpus) as tmp:
  stage=Path(tmp)
  for n in ['chunk_sets','catalog','review']: (stage/n).mkdir()
  for i,row in enumerate(sorted(rows,key=lambda x:x['openalex_id']),1):
   wid=row['openalex_id'];filename=row['source_filename'];digest=row['source_sha256']
   src=corpus/'pdfs'/filename
   pages=pdf_pages(src,max_pages=max_pages)
   overlap=title_match(row['title'],pages)
   if overlap<.64:raise ValueError(f'{wid} source/title page mismatch: {overlap}')
   uid=f'oa_{wid}_{digest[:12]}'
   pieces,tot=page_chunks(pages,uid,filename,digest,max_total=max_chunks)
   count=tot['chunk_count'];total_chunks+=count
   if total_chunks>max_chunks:raise ValueError('corpus chunk budget exceeded')
   date=row.get('date') or None
   year=int(date[:4]) if date and date[:4].isdigit() else None
   paper_meta={'paper_uid':uid,'paper_id':uid,'title':row['title'],
    'authors':[a for a in row.get('authors',[]) if isinstance(a,str) and a.strip()],
    'date':date,'year':year,'venue':row.get('venue') or None,
    'doi':row.get('doi') or None,
    'tags':[row['primary_pl']]+list(row.get('secondary_pls') or []),
    'source_file':filename,'source_sha256':digest,
    'source_ref':'sha256:'+digest,
    'source_kind':'registered_pdf_exact_bytes',
    'parser':PARSER,'openalex_work_id':wid,
    'pdf_version_note':'PDF manifestation SHA-anchored; edition not inferred'}
   f=write_chunk_set_artifact(pieces,source_items=[filename],run_id=uid,
       out_dir=stage/'chunk_sets',entrypoint='registered_pdf_page_chunk_v1',
       paper_meta=paper_meta)
   decoded=json.loads(f.read_text())
   assert decoded['chunk_count']==count and decoded['paper_meta']['source_sha256']==digest
   artifacts.append({'openalex_id':wid,'pdf':filename,'paper_uid':uid,
    'source_sha256':digest,'physical_pages':len(pages),
    'normalized_chars':tot['normalized_chars'],'pages_with_text':tot['pages_with_text'],
    'empty_pages':tot['pages_empty'],'chunk_count':count,
    'title_token_overlap':overlap,'chunk_set_sha256':sha(f)})
   print('PDF_CHUNK_OK',corpus.name,wid,f'{i}/{len(rows)}','pages',len(pages),
         'chunks',count,'title_match',overlap,flush=True)
  c=export_catalog_records(chunk_set_dir=stage/'chunk_sets',
       out_path=stage/'catalog/paper.catalog-record.v1.jsonl')
  r=export_review_records(chunk_set_dir=stage/'chunk_sets',
       out_path=stage/'review/paper.review-record.v1.jsonl')
  assert c['records']==r['records']==len(rows)
  receipt={**preflight,'papers':artifacts,'count_pdf':len(rows),
   'count_chunks':total_chunks,'catalog_sha256':c['sha256'],'review_sha256':r['sha256'],
   'no_grobid':True,'no_embeds':True,
   'headers_from_pure_pdf':'not_generated',
   'coverage_policy':'full normalized text for nonempty extractable pages; no OCR'}
  # Transactional swapping, preserving empty previous directories for rollback.
  rollback=stage/'rollback'
  rollback.mkdir()
  swapped=[]
  try:
   for n in ('chunk_sets','catalog','review'):
    target=corpus/n
    if target.exists():target.rename(rollback/n)
    (stage/n).rename(target)
    swapped.append(n)
   check=subprocess.run([sys.executable,'-m','pipeline.adapter.manager','doctor',
                        '--corpus',corpus.name,'--strict'],
                       cwd=ROOT,text=True,capture_output=True,timeout=110)
   if check.returncode!=0:raise RuntimeError('strict doctor failed: '+check.stdout[-1300:]+check.stderr[-350:])
   start=check.stdout.find("{")
   if start<0:raise RuntimeError("doctor returned no JSON payload: "+check.stdout[-700:])
   summary=json.loads(check.stdout[start:])
   validation=summary.get('chunk_set_validation') or {}
   if validation.get('n_pass')!=len(rows) or validation.get('n_fail')!=0 or summary.get('errors'):
    raise RuntimeError('doctor validation count or error: '+repr(summary)[-1000:])
   receipt['doctor']={'n_pdfs':summary['n_pdfs'],'n_pass':validation['n_pass'],
     'n_fail':validation['n_fail'],'n_xmls':summary['n_xmls'],
     'n_failures':summary['n_failures'],'ready_to_serve':summary.get('ready_to_serve'),
     'warnings':summary.get('warnings',[])[:3]}
  except Exception:
   for n in reversed(swapped):
    tgt=corpus/n
    if tgt.exists():tgt.rename(stage/('failed_'+n))
    if (rollback/n).exists():(rollback/n).rename(tgt)
   raise
  assert sha(corpus/'source-manifest.json')==preflight['source_manifest_sha256']
  for a in artifacts:
   assert sha(corpus/'pdfs'/a['pdf'])==a['source_sha256']
  return receipt
def main():
 a=argparse.ArgumentParser()
 a.add_argument('--plan',type=Path,required=True)
 a.add_argument('--corpus',required=True)
 a.add_argument('--receipt',type=Path)
 a.add_argument('--apply',action='store_true')
 a.add_argument('--max-files',type=int,default=32)
 a.add_argument('--max-bytes',type=int,default=120_000_000)
 a.add_argument('--max-pages',type=int,default=240)
 a.add_argument('--max-chunks',type=int,default=9500)
 opts=a.parse_args()
 corpus,rows,summary=plan(opts.corpus,opts.plan,opts.max_files,opts.max_bytes)
 if not opts.apply:
  print(json.dumps({**summary,'apply_required':True,'new_chunks':0},indent=2))
  print('DRY_RUN_PASS');return
 receipt=process(corpus,rows,summary,opts.max_pages,opts.max_chunks)
 if opts.receipt:
  opts.receipt.parent.mkdir(parents=True,exist_ok=True)
  opts.receipt.write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n')
 print('CORPUS_CHUNK_PASS',opts.corpus,receipt['count_pdf'],receipt['count_chunks'],flush=True)
if __name__=='__main__': main()
