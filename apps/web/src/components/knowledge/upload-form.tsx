"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Upload } from "lucide-react";
import { useTranslations } from "next-intl";
import * as React from "react";
import { toast } from "sonner";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { useSession } from "@/components/providers/session-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { api } from "@/lib/api/client";
import { useApiQuery } from "@/lib/api/hooks";
import { collectionSchema, uploadAcceptedSchema, workspaceSchema } from "@/lib/api/schemas";

/** Mirrors ragops.security.upload: the API re-checks type, size and content. */
export const ACCEPTED_TYPES = ".pdf,.docx,.md,.txt,.csv,.jpg,.jpeg,.png";
const SCAN_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png"];

export function maxUploadBytes(filename: string): number {
  const lower = filename.toLowerCase();
  return SCAN_EXTENSIONS.some((suffix) => lower.endsWith(suffix)) ? 12_000_000 : 2_000_000;
}

export function acceptedFile(filename: string): boolean {
  const lower = filename.toLowerCase();
  return ACCEPTED_TYPES.split(",").some((suffix) => lower.endsWith(suffix));
}

export function slugKey(title: string): string {
  const base = title
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return base || "document";
}

export function UploadForm() {
  const t = useTranslations("knowledge.upload");
  const { csrfToken } = useSession();
  const queryClient = useQueryClient();
  const workspaces = useApiQuery("/v1/workspaces", z.array(workspaceSchema));
  const [workspace, setWorkspace] = React.useState("");
  const collections = useApiQuery(workspace ? `/v1/collections?workspace_id=${workspace}` : null, z.array(collectionSchema));
  const [collection, setCollection] = React.useState("");
  const [title, setTitle] = React.useState("");
  const [file, setFile] = React.useState<File | null>(null);
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState<unknown>(null);
  const fileRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (!workspace && workspaces.data?.[0]) setWorkspace(workspaces.data[0].id);
  }, [workspace, workspaces.data]);
  React.useEffect(() => {
    const active = collections.data?.filter((item) => item.active);
    if (active && !active.some((item) => item.id === collection)) setCollection(active[0]?.id ?? "");
  }, [collection, collections.data]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const found: Record<string, string> = {};
    if (!workspace) found.workspace = t("errors.workspace");
    if (!collection) found.collection = t("errors.collection");
    if (!title.trim()) found.title = t("errors.title");
    if (!file) found.file = t("errors.file");
    else if (!acceptedFile(file.name)) found.file = t("errors.type");
    else if (file.size > maxUploadBytes(file.name)) found.file = t("errors.size");
    setErrors(found);
    if (Object.keys(found).length || !file) return;
    setBusy(true);
    setFailure(null);
    const form = new FormData();
    form.set("workspace_id", workspace);
    form.set("collection_id", collection);
    form.set("title", title.trim());
    form.set("logical_document_key", `${slugKey(title)}-${crypto.randomUUID().slice(0, 8)}`);
    form.set("file", file, file.name);
    try {
      await api.upload("/v1/documents/upload", form, uploadAcceptedSchema, { csrfToken });
      toast.success(t("accepted", { title: title.trim() }));
      setTitle("");
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      await queryClient.invalidateQueries({ queryKey: ["api", "/v1/ingestion/jobs"] });
    } catch (error) {
      setFailure(error);
    } finally {
      setBusy(false);
    }
  }

  // Choices load asynchronously; submitting before they are known would only fail validation.
  const choicesLoading = workspaces.isPending || (Boolean(workspace) && collections.isPending);

  const fieldError = (name: string) =>
    errors[name] ? (
      <p id={`${name}-error`} className="text-sm text-destructive">
        {errors[name]}
      </p>
    ) : null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Upload aria-hidden className="size-4 text-primary" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        <Alert tone="warning" className="mb-4">
          {t("syntheticOnly")}
        </Alert>
        {workspaces.isError ? <ApiErrorAlert error={workspaces.error} className="mb-4" /> : null}
        <form onSubmit={submit} className="grid grid-cols-1 gap-4 md:grid-cols-2" noValidate data-testid="upload-form">
          <div className="space-y-1.5">
            <Label htmlFor="workspace">{t("workspace")}</Label>
            <NativeSelect id="workspace" value={workspace} onChange={(event) => setWorkspace(event.target.value)} aria-invalid={errors.workspace ? true : undefined} aria-describedby={errors.workspace ? "workspace-error" : undefined}>
              {(workspaces.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </NativeSelect>
            {fieldError("workspace")}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="collection">{t("collection")}</Label>
            <NativeSelect id="collection" value={collection} onChange={(event) => setCollection(event.target.value)} aria-invalid={errors.collection ? true : undefined} aria-describedby={errors.collection ? "collection-error" : undefined}>
              {(collections.data ?? [])
                .filter((item) => item.active)
                .map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ({item.access_level})
                  </option>
                ))}
            </NativeSelect>
            {fieldError("collection")}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="doc-title">{t("documentTitle")}</Label>
            <Input id="doc-title" value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} aria-invalid={errors.title ? true : undefined} aria-describedby={errors.title ? "title-error" : undefined} data-testid="upload-title" />
            {fieldError("title")}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="doc-file">{t("file")}</Label>
            <Input
              ref={fileRef}
              id="doc-file"
              type="file"
              accept={ACCEPTED_TYPES}
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              aria-invalid={errors.file ? true : undefined}
              aria-describedby={errors.file ? "file-error" : "file-hint"}
              data-testid="upload-file"
            />
            {errors.file ? fieldError("file") : <p id="file-hint" className="text-xs text-muted-foreground">{t("fileHint")}</p>}
          </div>
          <div className="md:col-span-2">
            {failure ? <ApiErrorAlert error={failure} className="mb-3" /> : null}
            <Button type="submit" disabled={busy || choicesLoading} aria-busy={choicesLoading || undefined} data-testid="upload-submit">
              {busy ? <Loader2 aria-hidden className="animate-spin" /> : <Upload aria-hidden />}
              {busy ? t("uploading") : t("submit")}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
