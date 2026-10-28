"""Synthetic preflight assurance: no duplicate chunking / no mutation of existing sources."""
from pathlib import Path
import json,hashlib
import pytest
from scripts import append_registered_pdf_page_chunks as m

def h(data):
 return hashlib.sha256(data).hexdigest()

def sample(tmp,monkeypatch,selected='W2.pdf',tamper=False):
 monkeypatch.setattr(m,'ROOT',tmp)
 corpus=tmp/'corpora'/'test_chunk_append'
 for name in ['pdfs','chunk_sets','catalog','review']:
  (corpus/name).mkdir(parents=True)
 old=b'%PDF-old-immutable-test'
 new=b'%PDF-new-immutable-test'
 (corpus/'pdfs/W1.pdf').write_bytes(old)
 (corpus/'pdfs/W2.pdf').write_bytes(new)
 manifest={'corpus_id':'test_chunk_append','files':[
  dict(registered_filename='W1.pdf',sha256=h(old),size_bytes=len(old)),
  dict(registered_filename='W2.pdf',sha256=h(new),size_bytes=len(new))]}
 (corpus/'source-manifest.json').write_text(json.dumps(manifest))
 item={'artifact_family':'chunk_bus','artifact_kind':'chunk_set',
  'paper_meta':{'paper_uid':'paper_original'},
  'source_items':['W1.pdf'],'chunk_count':1}
 (corpus/'chunk_sets/paper_original.chunk_set.json').write_text(json.dumps(item))
 for section,filename in [('catalog','paper.catalog-record.v1.jsonl'),
                          ('review','paper.review-record.v1.jsonl')]:
  (corpus/section/filename).write_text(json.dumps({'paper_uid':'paper_original','title':'Old original'})+'\n')
 item={'openalex_id':selected.removesuffix('.pdf'),'source_corpus':'test_chunk_append',
  'source_filename':selected,'source_sha256':h(new) if not tamper else 'a'*64}
 plan=tmp/'plan.json'
 plan.write_text(json.dumps({'sources':[item]}))
 return corpus,plan,h(old)

def test_append_preflight_accepts_only_new_registered_source(tmp_path,monkeypatch):
 corpus,plan,oldhash=sample(tmp_path,monkeypatch)
 result=m.preflight('test_chunk_append',plan,30,10000)
 assert len(result[1])==1
 assert result[4]['old_count']==1
 assert (corpus/'pdfs/W1.pdf').read_bytes()==b'%PDF-old-immutable-test'
 assert m.sha(corpus/'pdfs/W1.pdf')==oldhash
 assert len(list((corpus/'chunk_sets').glob('*.json')))==1

def test_append_preflight_refuses_second_chunking_for_old_source(tmp_path,monkeypatch):
 _,plan,_=sample(tmp_path,monkeypatch,selected='W1.pdf')
 with pytest.raises(FileExistsError,match='already chunked'):
  m.preflight('test_chunk_append',plan,30,10000)

def test_append_preflight_rejects_source_sha_drift(tmp_path,monkeypatch):
 _,plan,_=sample(tmp_path,monkeypatch,tamper=True)
 with pytest.raises(ValueError,match='does not match manifest'):
  m.preflight('test_chunk_append',plan,30,10000)
