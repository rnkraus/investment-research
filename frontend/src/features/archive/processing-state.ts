import type { ProcessingRunResult } from "@/lib/api/client";

type ProcessingState = ProcessingRunResult["state"];

export function shouldPollProcessing(
  state: ProcessingState | undefined,
): boolean {
  return state !== "SEARCHABLE" && state !== "FAILED";
}

export function canResearchProcessingRun(
  state: ProcessingState | undefined,
): boolean {
  return state === "SEARCHABLE";
}
