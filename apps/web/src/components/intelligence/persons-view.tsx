"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Plus, UserPlus, Users } from "lucide-react";
import { useTranslations } from "next-intl";
import * as React from "react";
import { toast } from "sonner";
import { z } from "zod";
import { ApiErrorAlert } from "@/components/common/api-error";
import { StatusBadge } from "@/components/common/status-badge";
import { useSession } from "@/components/providers/session-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Alert, EmptyState, LoadingRows } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api } from "@/lib/api/client";
import { useApiQuery } from "@/lib/api/hooks";
import { aliasCreateSchema, personCreateSchema, personSchema } from "@/lib/api/schemas";
import { canReview } from "@/lib/roles";

const created = z.object({ id: z.string() }).passthrough();

function AliasDialog({ person, onClose }: { person: { id: string; label: string } | null; onClose: () => void }) {
  const t = useTranslations("intelligence.persons");
  const { csrfToken } = useSession();
  const queryClient = useQueryClient();
  const [kind, setKind] = React.useState<"name" | "identifier">("name");
  const [value, setValue] = React.useState("");
  const [error, setError] = React.useState<unknown>(null);
  const [invalid, setInvalid] = React.useState(false);
  React.useEffect(() => {
    setValue("");
    setError(null);
    setInvalid(false);
  }, [person]);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const parsed = aliasCreateSchema.safeParse({ kind, value });
    setInvalid(!parsed.success);
    if (!parsed.success || !person) return;
    try {
      await api.post(`/v1/intelligence/persons/${person.id}/aliases`, parsed.data, created, { csrfToken });
      toast.success(t("aliasAdded"));
      await queryClient.invalidateQueries({ queryKey: ["api", "/v1/intelligence/persons"] });
      onClose();
    } catch (failure) {
      setError(failure);
    }
  }
  return (
    <Dialog open={person !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent closeLabel={t("close")}>
        <form onSubmit={submit} className="space-y-4" noValidate>
          <DialogHeader>
            <DialogTitle>{t("addAlias")}</DialogTitle>
            <DialogDescription>{person?.label}</DialogDescription>
          </DialogHeader>
          <Alert tone="info">{t("aliasPrivacy")}</Alert>
          <div className="space-y-1.5">
            <Label htmlFor="alias-kind">{t("aliasKind")}</Label>
            <NativeSelect id="alias-kind" value={kind} onChange={(event) => setKind(event.target.value as "name" | "identifier")}>
              <option value="name">{t("kinds.name")}</option>
              <option value="identifier">{t("kinds.identifier")}</option>
            </NativeSelect>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="alias-value">{t("aliasValue")}</Label>
            <Input id="alias-value" value={value} maxLength={255} autoComplete="off" onChange={(event) => setValue(event.target.value)} aria-invalid={invalid || undefined} />
            {invalid ? <p className="text-sm text-destructive">{t("aliasInvalid")}</p> : null}
          </div>
          {error ? <ApiErrorAlert error={error} /> : null}
          <DialogFooter>
            <Button variant="outline" onClick={onClose}>
              {t("cancel")}
            </Button>
            <Button type="submit">{t("save")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function PersonsView() {
  const t = useTranslations("intelligence.persons");
  const session = useSession();
  const reviewer = canReview(session.roles);
  const queryClient = useQueryClient();
  const persons = useApiQuery("/v1/intelligence/persons", z.array(personSchema));
  const [label, setLabel] = React.useState("");
  const [error, setError] = React.useState<unknown>(null);
  const [invalid, setInvalid] = React.useState(false);
  const [alias, setAlias] = React.useState<{ id: string; label: string } | null>(null);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    const parsed = personCreateSchema.safeParse({ label });
    setInvalid(!parsed.success);
    if (!parsed.success) return;
    setError(null);
    try {
      await api.post("/v1/intelligence/persons", parsed.data, created, { csrfToken: session.csrfToken });
      toast.success(t("created"));
      setLabel("");
      await queryClient.invalidateQueries({ queryKey: ["api", "/v1/intelligence/persons"] });
    } catch (failure) {
      setError(failure);
    }
  }

  return (
    <div className="space-y-4">
      {reviewer ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <UserPlus aria-hidden className="size-4 text-primary" />
              {t("create")}
            </CardTitle>
            <CardDescription>{t("createHint")}</CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={create} className="flex flex-col gap-3 sm:flex-row sm:items-end" noValidate>
              <div className="flex-1 space-y-1.5">
                <Label htmlFor="person-label">{t("label")}</Label>
                <Input id="person-label" value={label} maxLength={120} onChange={(event) => setLabel(event.target.value)} aria-invalid={invalid || undefined} />
                {invalid ? <p className="text-sm text-destructive">{t("labelInvalid")}</p> : null}
              </div>
              <Button type="submit">
                <Plus aria-hidden />
                {t("createAction")}
              </Button>
            </form>
            {error ? <ApiErrorAlert error={error} className="mt-3" /> : null}
          </CardContent>
        </Card>
      ) : null}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users aria-hidden className="size-4 text-primary" />
            {t("title")}
          </CardTitle>
          <CardDescription>{t("hint")}</CardDescription>
        </CardHeader>
        <CardContent>
          {persons.isPending ? (
            <LoadingRows label={t("loading")} />
          ) : persons.isError ? (
            <ApiErrorAlert error={persons.error} />
          ) : persons.data.length === 0 ? (
            <EmptyState icon={Users} title={t("empty")} />
          ) : (
            <Table caption={t("title")}>
              <THead>
                <TR>
                  <TH>{t("label")}</TH>
                  <TH>{t("status")}</TH>
                  <TH>{t("aliases")}</TH>
                  {reviewer ? <TH className="text-right">{t("actions")}</TH> : null}
                </TR>
              </THead>
              <TBody>
                {persons.data.map((person) => (
                  <TR key={person.id}>
                    <TD className="font-medium">{person.label}</TD>
                    <TD>
                      <StatusBadge value={person.status} />
                    </TD>
                    <TD className="tabular">{person.alias_count}</TD>
                    {reviewer ? (
                      <TD className="text-right">
                        <Button variant="ghost" size="sm" onClick={() => setAlias({ id: person.id, label: person.label })}>
                          {t("addAlias")}
                        </Button>
                      </TD>
                    ) : null}
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
      <AliasDialog person={alias} onClose={() => setAlias(null)} />
    </div>
  );
}
