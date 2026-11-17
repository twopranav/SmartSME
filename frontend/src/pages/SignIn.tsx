import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, useMutation } from "@/lib/api";
import { AuthLayout } from "@/components/AuthLayout";
import { Icon } from "@/components/Icon";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";

const DEMO = { email: "demo@smartsme.app", password: "demo1234" };

/** A shared demo sign-in the server says exists on this instance. */
interface DemoAccount {
  key: string;
  label: string;
  hint: string;
  business: string;
  email: string;
  password: string;
}

export function SignIn() {
  const { run, pending, error } = useMutation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  // The two comparison shops are only offered where they have been loaded.
  const [shops, setShops] = useState<DemoAccount[]>([]);
  useEffect(() => {
    let live = true;
    api
      .get<{ accounts: DemoAccount[] }>("/auth/demo-accounts")
      .then((r) => live && setShops(r.accounts.filter((a) => a.key !== "main")))
      // No list is not an error worth showing: the first demo button still works.
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  function signIn(credentials: typeof DEMO) {
    return run(
      () => api.post("/auth/sign-in", credentials),
      // A full load re-runs the session probe in App with the new cookie.
      () => window.location.assign("/dashboard"),
    );
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    await signIn({ email, password });
  }

  async function useDemo(account: { email: string; password: string } = DEMO) {
    setEmail(account.email);
    setPassword(account.password);
    await signIn(account);
  }

  return (
    <AuthLayout>
      <h1 className="text-[1.75rem] font-bold tracking-[-0.03em]">Welcome back</h1>
      <p className="mt-2 text-sm text-muted-foreground">Sign in to your SmartSME workspace.</p>

      <form onSubmit={submit} className="mt-8 flex flex-col gap-4">
        <Field label="Email">
          <Input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@business.com"
            autoComplete="email"
            required
          />
        </Field>
        <Field label="Password">
          <div className="relative">
            <Input
              type={showPassword ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              autoComplete="current-password"
              className="pr-11"
              required
            />
            <button
              type="button"
              onClick={() => setShowPassword((shown) => !shown)}
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              tabIndex={-1}
              className="absolute inset-y-0 right-0 flex w-11 items-center justify-center rounded-r-xl text-muted-foreground transition-colors hover:text-foreground focus-visible:focus-ring"
            >
              <Icon name={showPassword ? "eyeOff" : "eye"} size={17} />
            </button>
          </div>
        </Field>
        {error && (
          <p
            role="alert"
            className="flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            <Icon name="alert" size={16} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </p>
        )}
        <Button type="submit" size="lg" className="mt-2 w-full" disabled={pending}>
          {pending ? "Signing in…" : "Sign in"}
        </Button>
      </form>

      <button
        type="button"
        onClick={() => useDemo()}
        disabled={pending}
        className="mt-4 w-full rounded-lg py-1 text-sm font-semibold text-link transition-opacity hover:opacity-70 focus-visible:focus-ring disabled:opacity-50"
      >
        Use the demo account
      </button>

      {shops.length > 0 && (
        <div className="mt-3 rounded-xl border border-border p-3">
          <p className="text-center text-xs text-muted-foreground">
            Or compare two shops with four months of trading each
          </p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {shops.map((shop) => (
              <button
                key={shop.key}
                type="button"
                onClick={() => useDemo(shop)}
                disabled={pending}
                title={`${shop.business}: ${shop.hint}`}
                className="rounded-lg border border-border px-3 py-2 text-left transition-colors hover:bg-muted focus-visible:focus-ring disabled:opacity-50"
              >
                <span className="block text-sm font-semibold text-link">{shop.label}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {shop.business}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      <p className="mt-6 text-center text-sm text-muted-foreground">
        New here?{" "}
        <Link to="/sign-up" className="font-semibold text-link hover:underline">
          Create an account
        </Link>
      </p>
    </AuthLayout>
  );
}
