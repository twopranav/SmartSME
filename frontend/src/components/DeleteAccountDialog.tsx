import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, useMutation } from "@/lib/api";
import { Modal } from "./ui/modal";
import { Button } from "./ui/button";
import { Field, Input } from "./ui/input";
import { Icon } from "./Icon";
import { describeRecords } from "@/lib/account";

interface DeletionPlan {
  allowed: boolean;
  reason: string | null;
  deletesBusiness: boolean;
  businessName: string | null;
  otherMembers: number;
  records: Record<string, number>;
}

/**
 * Deleting your own account. The server says what would go with it, so the
 * warning names the business, its records and the other people who would lose
 * access, rather than a generic "are you sure". The password is asked for
 * again because a session left open on a shop computer should not be enough.
 */
export function DeleteAccountDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const { run, pending, error } = useMutation();
  const [plan, setPlan] = useState<DeletionPlan | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [password, setPassword] = useState("");

  useEffect(() => {
    if (!open) return;
    let live = true;
    setPlan(null);
    setLoadError(null);
    setPassword("");
    api
      .get<DeletionPlan>("/auth/account-deletion")
      .then((p) => live && setPlan(p))
      .catch((e: unknown) => live && setLoadError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [open]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    await run(async () => {
      await api.post("/auth/delete-account", { password });
      navigate("/sign-in", { replace: true });
    });
  }

  const records = plan ? describeRecords(plan.records) : "";

  return (
    <Modal open={open} onClose={onClose} title="Delete account" className="max-w-md">
      {loadError && <p className="text-sm text-destructive">{loadError}</p>}
      {!plan && !loadError && <p className="text-sm text-muted-foreground">Checking…</p>}

      {plan && !plan.allowed && (
        <div className="flex flex-col gap-4">
          <p className="text-sm text-muted-foreground">{plan.reason}</p>
          <div className="flex justify-end">
            <Button type="button" variant="outline" onClick={onClose}>
              Close
            </Button>
          </div>
        </div>
      )}

      {plan && plan.allowed && (
        <form onSubmit={submit} className="flex flex-col gap-4">
          {plan.deletesBusiness ? (
            <div className="flex gap-3 rounded-lg bg-destructive/10 px-4 py-3 text-sm text-destructive">
              <Icon name="alert" size={18} className="mt-0.5 shrink-0" />
              <div className="space-y-1.5">
                <p className="font-semibold">
                  This deletes {plan.businessName ?? "your business"} as well.
                </p>
                <p>
                  You are its only owner, so the business goes with your account
                  {records ? `: ${records}` : ""}.
                  {plan.otherMembers > 0 &&
                    ` ${plan.otherMembers} other team ${
                      plan.otherMembers === 1 ? "member loses" : "members lose"
                    } access too.`}
                </p>
                <p>This cannot be undone. Download a report first if you need the records.</p>
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              Your sign-in is removed from {plan.businessName ?? "the business"}. The sales,
              purchases and expenses you recorded stay with the business. This cannot be undone.
            </p>
          )}

          <Field label="Your password" hint="Type it again to confirm it is you.">
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>

          {error && <p className="text-sm text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={onClose} disabled={pending}>
              Cancel
            </Button>
            <Button type="submit" variant="destructive" disabled={pending || !password}>
              {pending
                ? "Deleting…"
                : plan.deletesBusiness
                  ? "Delete account and business"
                  : "Delete my account"}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
