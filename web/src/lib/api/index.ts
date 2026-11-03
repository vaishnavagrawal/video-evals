"use client";
import { mockApi } from "./mock";
import { realApi } from "./client";
import type { VreviewApi } from "./types";

export const IS_MOCK = process.env.NEXT_PUBLIC_VREVIEW_MOCK === "1";
export const api: VreviewApi = IS_MOCK ? mockApi : realApi;
export * from "./types";
