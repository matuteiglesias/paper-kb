import type { components } from '../../src/api/generated';

// Additive contract surface until the legacy generated.ts OpenAPI client is regenerated.
// Keep this mapping aligned with backend/app/schemas.py and API contract tests.
export type PaperMeta = components['schemas']['PaperMeta'] & { paper_uid?: string | null };
export type PapersList = Omit<components['schemas']['PapersList'], 'papers'> & {
  papers: PaperMeta[];
  total?: number | null;
};
export type ChunkResponse = components['schemas']['ChunkResponse'] & {
  paper_id?: string | null;
  paper_uid?: string | null;
};
export type CanonicalChunk = components['schemas']['CanonicalChunk'] & { paper_uid?: string | null };
export type PaperChunksResponse = Omit<components['schemas']['PaperChunksResponse'], 'chunks'> & {
  chunks: ChunkResponse[];
};
export type SearchRequest = components['schemas']['SearchRequest'];
export type SearchResponse = Omit<components['schemas']['SearchResponse'], 'hits'> & {
  hits: (components['schemas']['SearchHit'] & { paper_uid?: string | null })[];
};
export type HTTPValidationError = components['schemas']['HTTPValidationError'];

export type CorpusInfoResponse = {
  corpus_name?: string | null;
  storage_backend: string;
  chunk_sets_dir?: string | null;
  cache_ready: boolean;
  loaded_at?: number | null;
};

export type CorpusHealthResponse = {
  status: string;
  n_papers: number;
  n_chunks: number;
  n_artifacts: number;
  n_invalid_artifacts: number;
  n_skipped_chunks?: number;
  dedupe_collisions?: number;
  warnings: string[];
};


export type PaperSummary = {
  paper_id: string;
  title: string;
  summary_version: number;
  generated_at: string;
  provider: string;
  model: string;
  source: {
    corpus: string;
    chunk_set_dir: string;
    n_chunks_total: number;
    n_chunks_selected: number;
    selected_chunk_ids: string[];
  };
  status: string;
  one_line: string;
  research_question: string;
  data: string;
  method: string;
  main_contribution: string;
  limitations: string;
  relevance_to_thesis: string;
  suggested_tags: { method_tags: string[]; data_tags: string[] };
  confidence: "low" | "medium" | "high";
  warnings: string[];
};

export type SummaryGenerateRequest = { provider?: "mock" | "agent-framework"; force?: boolean };
