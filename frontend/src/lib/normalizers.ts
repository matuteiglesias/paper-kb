import type {
  PaperMeta as ApiPaperMeta,
  PapersList as ApiPapersList,
  ChunkResponse as ApiChunkResponse,
  PaperChunksResponse as ApiPaperChunksResponse,
  SearchResponse as ApiSearchResponse,
} from "@/api/types";

export type RawPaperMeta = ApiPaperMeta;
export type RawPapersList = ApiPapersList;
export type RawChunkResponse = ApiChunkResponse;
export type RawPaperChunksResponse = ApiPaperChunksResponse;
export type RawSearchHit = ApiSearchResponse["hits"][number];

type ExtendedRawPaperMeta = RawPaperMeta & {
  paper_uid?: string | null;
  abstract?: string | null;
  date?: string | null;
  year?: number | null;
  venue?: string | null;
  doi?: string | null;
  arxiv_id?: string | null;
  tags?: string[] | null;
  created_at?: string | null;
  pipeline_version?: string | null;
  embed_model?: string | null;
  status?: string | null;
};

type ExtendedRawChunk = RawChunkResponse & { paper_id?: string | null };
type ExtendedPaperChunksResponse = RawPaperChunksResponse & { paper_id?: string | null };

export type PaperMeta = {
  paperId: string;
  paperUid?: string | null;
  title: string;
  authors: string[];
  nChunks: number;
  preview?: string | null;
  pages?: number | null;
  sourceFile?: string | null;
  abstract?: string | null;
  date?: string | null;
  year?: number | null;
  venue?: string | null;
  doi?: string | null;
  arxivId?: string | null;
  tags: string[];
  createdAt?: string | null;
  pipelineVersion?: string | null;
  embedModel?: string | null;
  status?: string | null;
};

export type Chunk = {
  id: string;
  paperId?: string;
  paperUid?: string | null;
  pos: number;
  text: string;
  charLen: number;
  headerPath?: string[] | null;
  pages?: (number | null)[] | null;
  meta?: Record<string, unknown> | null;
};

export type PaperChunksNormalized = {
  paperId: string;
  total: number;
  chunks: Chunk[];
};

export function normalizePaperMeta(raw: RawPaperMeta): PaperMeta {
  const r: ExtendedRawPaperMeta = raw;
  return {
    paperId: r.paper_id,
    paperUid: r.paper_uid ?? null,
    title: r.title,
    authors: Array.isArray(r.authors) ? r.authors : [],
    nChunks: r.n_chunks,
    preview: r.preview ?? null,
    pages: r.pages ?? null,
    sourceFile: r.source_file ?? null,
    abstract: r.abstract ?? null,
    date: r.date ?? null,
    year: r.year ?? null,
    venue: r.venue ?? null,
    doi: r.doi ?? null,
    arxivId: r.arxiv_id ?? null,
    tags: Array.isArray(r.tags) ? r.tags : [],
    createdAt: r.created_at ?? null,
    pipelineVersion: r.pipeline_version ?? null,
    embedModel: r.embed_model ?? null,
    status: r.status ?? null,
  };
}

export function normalizeChunkResponse(raw: RawChunkResponse, parentPaperId?: string): Chunk {
  const r: ExtendedRawChunk = raw;
  return {
    id: r.chunk_id,
    paperId: parentPaperId ?? r.paper_id ?? undefined,
    paperUid: r.paper_uid ?? null,
    pos: typeof r.chunk_index === "number" ? r.chunk_index : 0,
    text: r.text ?? "",
    charLen: typeof r.char_len === "number" ? r.char_len : (r.text ? r.text.length : 0),
    headerPath: r.header_path ?? null,
    pages: Array.isArray(r.pages) ? [r.pages[0], r.pages[1]] : null,
    meta: r.meta ?? null,
  };
}

export function normalizePapersList(raw: RawPapersList): PaperMeta[] {
  if (!raw || !Array.isArray(raw.papers)) return [];
  const mapped = raw.papers.map((paper) => normalizePaperMeta(paper));
  mapped.sort((a, b) =>
    String(a.title || "").localeCompare(String(b.title || "")) ||
    a.paperId.localeCompare(b.paperId)
  );
  return mapped;
}

export function normalizePaperChunksResp(raw: RawPaperChunksResponse): PaperChunksNormalized {
  const r: ExtendedPaperChunksResponse = raw;
  const topPaperId = r.paper_id ?? "";
  const total = typeof r.total === "number" ? r.total : (Array.isArray(r.chunks) ? r.chunks.length : 0);
  const chunks: Chunk[] = Array.isArray(r.chunks)
    ? r.chunks.map((chunk) => normalizeChunkResponse(chunk, topPaperId))
    : [];
  return { paperId: topPaperId, total, chunks };
}
