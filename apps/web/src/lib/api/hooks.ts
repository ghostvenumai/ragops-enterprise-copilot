"use client";

import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import type { z } from "zod";
import { api } from "./client";

/** Query keyed by API path; locale is deliberately not part of the key (data is language-neutral). */
export function useApiQuery<T>(
  path: string | null,
  schema: z.ZodType<T>,
  options: Omit<UseQueryOptions<T>, "queryKey" | "queryFn"> & { allowStatus?: number } = {},
) {
  const { allowStatus, ...rest } = options;
  return useQuery<T>({
    queryKey: ["api", path],
    queryFn: ({ signal }) => api.get(path as string, schema, { signal, allowStatus }),
    enabled: path !== null && (rest.enabled ?? true),
    ...rest,
  });
}
