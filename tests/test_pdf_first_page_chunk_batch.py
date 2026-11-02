"""Synthetic unit tests; never inspect or publish private source text."""
import hashlib,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from build_registered_pdf_page_chunks import page_chunks,title_match

def test_chunk_page_boundaries_identity_and_full_normalized_text():
 source='e'*64
 page1=('Synthetic evidence about firms and economic shocks.\n' * 110).strip()
 page2='\n  Illustrative last page.  '
 uid='oa_W12345678_'+source[:12]
 chunks,tot=page_chunks([page1,'',page2],uid,'W12345678.pdf',source,max_chars=500)
 assert len(chunks)>10 and tot['pages_with_text']==2 and tot['pages_empty']==1
 assert len({x['chunk_id'] for x in chunks})==len(chunks)
 for page,raw in [(1,page1),(3,page2)]:
  texts=[c['text'] for c in chunks if c['metadata']['page_start']==page]
  assert ' '.join(texts)==' '.join(raw.split())
  assert all(c['source_ref']=='sha256:'+source for c in chunks)
  assert all(c['paper_uid']==uid for c in chunks)
  assert all(c['metadata']['page_end']==page for c in chunks if c['metadata']['page_start']==page)
  assert all(c['metadata']['page_char_end']>c['metadata']['page_char_start'] for c in chunks)

def test_changed_pdf_hash_changes_chunk_ids():
 pages=['Synthetic economic source page. ' * 70]
 one,_=page_chunks(pages,'oa_a_aaa','a.pdf','a'*64)
 two,_=page_chunks(pages,'oa_b_bbb','a.pdf','b'*64)
 assert {x['chunk_id'] for x in one}.isdisjoint(x['chunk_id'] for x in two)

def test_nonselectable_pdf_and_bad_long_token_fail_closed():
 for pages in (['','   '],['Z'*1700]):
  try:page_chunks(pages,'uid','x.pdf','a'*64,max_chars=500)
  except ValueError:pass
  else:raise AssertionError('unexpected successful empty/long token extraction')

def test_title_witness_checks_physical_first_pages():
 assert title_match('Granular Firm Growth', ['Granular Firm Growth\n An analysis', 'More results'])==1.0
 assert title_match('Granular Firm Growth',['Other data with no related terms',''])<.64
