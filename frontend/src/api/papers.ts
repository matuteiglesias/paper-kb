import type {
  PapersList,
  PaperChunksResponse,
  HTTPValidationError,
  PaperMeta,
  CorpusInfoResponse,
  CorpusHealthResponse,
  PaperSummary,
  SummaryGenerateRequest,
} from "@/api/types";
import { apiGet, apiPost } from "@/lib/api";

export type SearchHit = {
  id: string;
  text: string;
  score?: number | null;
  meta?: Record<string, unknown> | null;
  chunk_id?: string | null;
  paper_id?: string | null;
  paper_uid?: string | null;
};

export type SearchResults = {
  capability: string;
  query: string;
  k: number;
  hits: SearchHit[];
};

type ApiValidationError = Error & {
  status?: number;
  body?: unknown;
  validation?: HTTPValidationError;
};

async function typedGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  try {
    return await apiGet<T>(path, { signal });
  } catch (error: unknown) {
    const err = error as ApiValidationError;
    if (err.status === 422) {
      err.validation = (err.body || {}) as HTTPValidationError;
    }
    throw err;
  }
}

export async function fetchCorpusInfo(signal?: AbortSignal): Promise<CorpusInfoResponse> {
  return typedGet<CorpusInfoResponse>("/api/corpus", signal);
}

export async function fetchCorpusHealth(signal?: AbortSignal): Promise<CorpusHealthResponse> {
  return typedGet<CorpusHealthResponse>("/api/corpus/health", signal);
}

export async function fetchPapers(signal?: AbortSignal): Promise<PapersList> {
  const pageSize = 200;
  const first = await typedGet<PapersList>(`/api/papers?offset=0&limit=${pageSize}`, signal);
  if (first.total == null || first.papers.length >= first.total) return first;

  // The Workbench expects the full paper catalog; do not silently treat page 1
  // as complete. Missing progress or duplicate IDs indicates a changed view.
  const papers = [...first.papers];
  const seen = new Set(papers.map((paper) => paper.paper_id));
  while (papers.length < first.total) {
    const batch = await typedGet<PapersList>(
      `/api/papers?offset=${papers.length}&limit=${pageSize}`, signal
    );
    if (!batch.papers.length || batch.total !== first.total ||
        batch.papers.some((paper) => seen.has(paper.paper_id))) {
      throw new Error("Corpus listing changed during pagination; retry the request");
    }
    for (const paper of batch.papers) {
      papers.push(paper);
      seen.add(paper.paper_id);
    }
  }
  return { ...first, papers };
}

export async function fetchPaper(paperId: string, signal?: AbortSignal): Promise<PaperMeta> {
  return typedGet<PaperMeta>(`/api/papers/${encodeURIComponent(paperId)}`, signal);
}

export async function fetchPaperChunks(
  paperId: string,
  offset = 0,
  limit = 50,
  signal?: AbortSignal
): Promise<PaperChunksResponse> {
  const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
  return typedGet<PaperChunksResponse>(`/api/papers/${encodeURIComponent(paperId)}/chunks?${params}`, signal);
}

export async function searchPaperChunks(
  q: string,
  opts?: { k?: number; paperId?: string | null },
  signal?: AbortSignal
): Promise<SearchResults> {
  return apiPost<SearchResults>(
    "/api/search",
    { q, k: opts?.k ?? 20, paper_id: opts?.paperId || null },
    { signal }
  );
}

export async function fetchPaperSummary(paperId: string, signal?: AbortSignal): Promise<PaperSummary> {
  return typedGet<PaperSummary>(`/api/papers/${encodeURIComponent(paperId)}/summary`, signal);
}

export async function generatePaperSummary(
  paperId: string,
  body: SummaryGenerateRequest = { provider: "agent-framework", force: false },
  signal?: AbortSignal
): Promise<PaperSummary> {
  return apiPost<PaperSummary>(`/api/papers/${encodeURIComponent(paperId)}/summary:generate`, body, { signal });
}
