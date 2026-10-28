#!/usr/bin/env python3
"""Safely ADD selected PDF-first chunks while retaining existing Paper KB identities.

Never overwrite source manifests, PDFs or extant chunk bytes. Stage full merged
projection, check existing paper identities and old catalog/review row values,
swap derivatives with rollback on strict corpus doctor failures.
"""
from __future__ import annotations
import argparse,hashlib,json,shutil,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.build_registered_pdf_page_chunks import (sha,pdf_pages,page_chunks,title_match,PARSER)
from pipeline.writers.chunk_set_writer import write_chunk_set_artifact
from pipeline.projections.catalog_records import export_catalog_records
from pipeline.projections.review_records import export_review_records

def recordlines(p):
 return {r['paper_uid']:r for r in (json.loads(x) for x in p.read_text().splitlines() if x.strip())}
def prior_refs(corpus):
 out={}
 for p in (corpus/'chunk_sets').glob('*.chunk_set.json'):
  j=json.loads(p.read_text())
  assert j['artifact_family']=='chunk_bus' and j['artifact_kind']=='chunk_set'
  uid=j['paper_meta']['paper_uid']
  if uid in out:raise ValueError('ambiguous existing paper UID')
  out[uid]={'file':p.name,'sha256':sha(p),'source_items':j['source_items'],'chunks':j['chunk_count']}
 return out
def preflight(name,plan,max_files,max_bytes):
 corpus=ROOT/'corpora'/name
 if not corpus.is_dir() or corpus.is_symlink():raise ValueError('corpus directory not safe')
 info=json.loads((corpus/'source-manifest.json').read_text())
 if info['corpus_id']!=name:raise ValueError('corpus identity mismatch')
 items=[x for x in json.loads(plan.read_text())['sources'] if x['source_corpus']==name]
 if not 1<=len(items)<=max_files:raise ValueError(f'bounded corpus item count {len(items)}')
 old=prior_refs(corpus)
 if not old:raise ValueError('append tool requires existing chunk sets')
 old_sources={s for x in old.values() for s in x['source_items']}
 old_uids=set(old)
 manifest={x['registered_filename']:x for x in info['files']}
 total=0
 for x in items:
  name=x['source_filename'];wid=x['openalex_id']
  assert name==wid+'.pdf' and name in manifest
  if name in old_sources:raise FileExistsError('source already chunked: '+name)
  pdf=corpus/'pdfs'/name
  if not pdf.is_file() or pdf.is_symlink():raise ValueError('bad source: '+name)
  if pdf.stat().st_size!=manifest[name]['size_bytes'] or sha(pdf)!=manifest[name]['sha256'] or x['source_sha256']!=manifest[name]['sha256']:
   raise ValueError('selected source does not match manifest: '+name)
  uid='oa_'+wid+'_'+x['source_sha256'][:12]
  if uid in old_uids:raise ValueError('paper UID already exists: '+uid)
  total+=pdf.stat().st_size
 if total>max_bytes:raise ValueError('PDF byte budget exceeded')
 expected_old={kind:recordlines(corpus/kind/('paper.catalog-record.v1.jsonl' if kind=='catalog' else 'paper.review-record.v1.jsonl')) for kind in ('catalog','review')}
 if len(expected_old['catalog'])!=len(old) or set(expected_old['catalog'])!=set(old):raise ValueError('existing catalog not complete')
 if len(expected_old['review'])!=len(old) or set(expected_old['review'])!=set(old):raise ValueError('existing review not complete')
 return corpus,items,old,expected_old,dict(corpus=name,old_count=len(old),increment=len(items),
   source_bytes=total,source_manifest_sha256=sha(corpus/'source-manifest.json'),plan_sha256=sha(plan))
def apply(corpus,items,old,original,meta,max_pages,max_chunks):
 added=[];count=0;source_bytes=0
 with tempfile.TemporaryDirectory(prefix='.append_chunk_stage_',dir=corpus) as tmpdir:
  stage=Path(tmpdir)
  for kind in ('chunk_sets','catalog','review'):(stage/kind).mkdir()
  for uid,r in old.items():
   p=corpus/'chunk_sets'/r['file']
   assert sha(p)==r['sha256']
   shutil.copy2(p,stage/'chunk_sets'/p.name)
  for i,x in enumerate(sorted(items,key=lambda r:r['openalex_id']),1):
   wid=x['openalex_id'];fn=x['source_filename'];pdf=corpus/'pdfs'/fn
   source_sha=x['source_sha256']
   pages=pdf_pages(pdf,max_pages=max_pages)
   overlap=title_match(x['title'],pages)
   if overlap<.64:raise ValueError(f'PDF title mismatch {wid} {overlap}')
   uid='oa_'+wid+'_'+source_sha[:12]
   chunks,tot=page_chunks(pages,uid,fn,source_sha,max_total=max_chunks)
   if count+len(chunks)>max_chunks:raise ValueError('maximum added chunks exceeded')
   date=x.get('date') or None
   pm={'paper_uid':uid,'paper_id':uid,'title':x['title'],
     'authors':[s for s in x.get('authors',[]) if isinstance(s,str) and s.strip()],
     'date':date,'year':int(date[:4]) if date and date[:4].isdigit() else None,
     'venue':x.get('venue') or None,'doi':x.get('doi') or None,
     'tags':[x['primary_pl']]+list(x.get('secondary_pls') or []),
     'source_file':fn,'source_sha256':source_sha,
     'source_ref':'sha256:'+source_sha,'source_kind':'registered_pdf_exact_bytes',
     'parser':PARSER,'openalex_work_id':wid,
     'pdf_version_note':'PDF manifestation SHA-anchored; edition not inferred'}
   path=write_chunk_set_artifact(chunks,source_items=[fn],run_id=uid,
     out_dir=stage/'chunk_sets',entrypoint='registered_pdf_page_chunk_v1',paper_meta=pm)
   j=json.loads(path.read_text())
   assert j['chunk_count']==len(chunks) and pm['paper_uid']==j['paper_meta']['paper_uid']
   count+=len(chunks);source_bytes+=pdf.stat().st_size
   added.append({'openalex_id':wid,'pdf':fn,'paper_uid':uid,'source_sha256':source_sha,
     'chunk_set_sha256':sha(path),'pages':len(pages),'page_chunks':len(chunks),
     'pages_with_text':tot['pages_with_text'],'normalized_chars':tot['normalized_chars'],
     'title_overlap':overlap})
   print('APPEND_PDF_OK',corpus.name,wid,f'{i}/{len(items)}','pages',len(pages),'chunks',len(chunks),flush=True)
  assert len(added)==len(items)
  assert len(list((stage/'chunk_sets').glob('*.chunk_set.json')))==len(old)+len(items)
  for uid,r in old.items():
   assert sha(stage/'chunk_sets'/r['file'])==r['sha256']
  pc=export_catalog_records(chunk_set_dir=stage/'chunk_sets',out_path=stage/'catalog/paper.catalog-record.v1.jsonl')
  pr=export_review_records(chunk_set_dir=stage/'chunk_sets',out_path=stage/'review/paper.review-record.v1.jsonl')
  expected=len(old)+len(items)
  assert pc['records']==pr['records']==expected
  for kind in ('catalog','review'):
   p=stage/kind/('paper.catalog-record.v1.jsonl' if kind=='catalog' else 'paper.review-record.v1.jsonl')
   combined=recordlines(p)
   assert len(combined)==expected
   for uid,original_record in original[kind].items():
    assert combined[uid]==original_record, f'{kind} existing record mutated for {uid}'
  rollback=stage/'rollback';rollback.mkdir()
  swapped=[]
  try:
   for kind in ('chunk_sets','catalog','review'):
    t=corpus/kind
    if not t.exists() or t.is_symlink():raise ValueError('missing or symlink destination '+str(t))
    t.rename(rollback/kind)
    (stage/kind).rename(t)
    swapped.append(kind)
   p=subprocess.run([sys.executable,'-m','pipeline.adapter.manager','doctor','--corpus',corpus.name,'--strict'],
       cwd=ROOT,text=True,capture_output=True,timeout=120)
   if p.returncode!=0:raise RuntimeError('strict doctor failed: '+p.stdout[-1000:]+p.stderr[-300:])
   start=p.stdout.find('{')
   if start<0:raise RuntimeError('doctor missing JSON')
   summary=json.loads(p.stdout[start:])
   diag=summary.get('chunk_set_validation') or {}
   if diag.get('n_pass')!=expected or diag.get('n_fail') or summary.get('errors') or not summary.get('ready_to_serve'):
    raise RuntimeError('strict doctor count/source mismatch: '+str(diag))
   for uid,r in old.items():
    assert sha(corpus/'chunk_sets'/r['file'])==r['sha256'],('old identity drift',uid)
   assert sha(corpus/'source-manifest.json')==meta['source_manifest_sha256']
   for r in added:assert sha(corpus/'pdfs'/r['pdf'])==r['source_sha256']
  except Exception:
   for kind in reversed(swapped):
    target=corpus/kind
    if target.exists():target.rename(stage/('failed_'+kind))
    if (rollback/kind).exists():(rollback/kind).rename(target)
   raise
  return {**meta,'added':added,'new_count':len(added),'new_chunks':count,
    'previous_count':len(old),'merged_count':expected,
    'old_identity_sha_preserved':len(old),'old_catalog_review_records_preserved':True,
    'new_source_bytes':source_bytes,
    'catalog_sha256':pc['sha256'],'review_sha256':pr['sha256'],
    'strict_doctor':{'n_pass':diag['n_pass'],'n_fail':diag.get('n_fail',0),
      'ready_to_serve':summary['ready_to_serve']},'no_embeddings':True,'no_source_mutation':True}
def main():
 a=argparse.ArgumentParser()
 a.add_argument('--plan',type=Path,required=True)
 a.add_argument('--corpus',required=True)
 a.add_argument('--receipt',type=Path)
 a.add_argument('--apply',action='store_true')
 a.add_argument('--max-files',type=int,default=30)
 a.add_argument('--max-bytes',type=int,default=120000000)
 a.add_argument('--max-pages',type=int,default=240)
 a.add_argument('--max-chunks',type=int,default=10000)
 x=a.parse_args()
 corpus,items,old,original,meta=preflight(x.corpus,x.plan,x.max_files,x.max_bytes)
 if not x.apply:
  print(json.dumps({**meta,'dry_run':True},indent=2));print('APPEND_DRY_RUN_PASS');return
 receipt=apply(corpus,items,old,original,meta,x.max_pages,x.max_chunks)
 if x.receipt:
  x.receipt.parent.mkdir(parents=True,exist_ok=True)
  x.receipt.write_text(json.dumps(receipt,indent=2,ensure_ascii=False)+'\n')
 print('APPEND_CORPUS_PASS',x.corpus,'new',receipt['new_count'],
   'merged',receipt['merged_count'],'new_chunks',receipt['new_chunks'],flush=True)
if __name__=='__main__':main()
