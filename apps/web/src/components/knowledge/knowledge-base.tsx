"use client";

import { FolderTree } from "lucide-react";
import { useTranslations } from "next-intl";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { PageHeader } from "@/components/common/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, LoadingRows } from "@/components/ui/feedback";
import { useApiQuery } from "@/lib/api/hooks";
import { collectionSchema, workspaceSchema } from "@/lib/api/schemas";
import { JobsTable } from "./jobs-table";
import { UploadForm } from "./upload-form";

function Structure() {
  const t = useTranslations("knowledge.structure");
  const workspaces = useApiQuery("/v1/workspaces", z.array(workspaceSchema));
  const collections = useApiQuery("/v1/collections", z.array(collectionSchema));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderTree aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {workspaces.isPending || collections.isPending ? (
          <LoadingRows label={t("loading")} rows={3} />
        ) : workspaces.isError ? (
          <ApiErrorAlert error={workspaces.error} />
        ) : collections.isError ? (
          <ApiErrorAlert error={collections.error} />
        ) : workspaces.data.length === 0 ? (
          <EmptyState icon={FolderTree} title={t("empty")} />
        ) : (
          <ul className="space-y-3" data-testid="workspaces">
            {workspaces.data.map((workspace) => (
              <li key={workspace.id}>
                <p className="text-sm font-semibold">{workspace.name}</p>
                <ul className="mt-1 space-y-1 border-l pl-3">
                  {collections.data
                    .filter((item) => item.workspace_id === workspace.id)
                    .map((item) => (
                      <li key={item.id} className="flex flex-wrap items-center gap-2 text-sm">
                        <span>{item.name}</span>
                        <Badge tone="neutral">{item.access_level}</Badge>
                        {!item.active ? <Badge tone="warning">{t("inactive")}</Badge> : null}
                      </li>
                    ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

export function KnowledgeBase() {
  const t = useTranslations("knowledge");
  return (
    <>
      <PageHeader title={t("title")} description={t("description")} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <UploadForm />
          <JobsTable />
        </div>
        <Structure />
      </div>
    </>
  );
}
