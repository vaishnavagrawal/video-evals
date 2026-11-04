/** Workflow stages as written to the run row by src/vreview/workflows/review.py (_stage), in DAG order. */
export const STAGES = [
  { id: "ingest", label: "Ingest media" },
  { id: "dubbing_plan", label: "Plan dub tracks" },
  { id: "dubbing_features", label: "Extract features (ASR, separation)" },
  { id: "dubbing_pairs", label: "Compare original and dub" },
  { id: "assemble", label: "Assemble review" },
  { id: "av_judge", label: "Audio-video judge" },
  { id: "finalize", label: "Triage and finalize" },
] as const;

export function stageIndex(stage: string | null | undefined): number {
  if (stage === "completed") return STAGES.length;
  return STAGES.findIndex((s) => s.id === stage);
}

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "Waiting";
  if (stage === "completed") return "Completed";
  return STAGES.find((s) => s.id === stage)?.label ?? stage;
}
