import { Button } from "./ui/button";
import { Field, Input } from "./ui/input";
import { Icon } from "./Icon";
import { money } from "@/lib/utils";

/**
 * "Amount paid" with a one-press way to say the whole bill was settled.
 *
 * Paid in full is a switch, not a one-off fill: while it is on the amount
 * follows the total, so adding another line does not leave the old figure
 * behind. Typing an amount turns it off again, which is how a part payment or
 * a credit sale (leave it empty) is entered.
 */
export function AmountPaidField({
  total,
  currency,
  typed,
  onTyped,
  paidInFull,
  onPaidInFull,
  hint,
}: {
  /** What the document will come to, as it will be stored. */
  total: number;
  currency: string;
  typed: number;
  onTyped: (amount: number) => void;
  paidInFull: boolean;
  onPaidInFull: (on: boolean) => void;
  hint: string;
}) {
  const shown = paidInFull ? total : typed;
  return (
    <Field
      label="Amount paid"
      hint={paidInFull ? `Marked as paid in full: ${money(total, currency)}.` : hint}
    >
      <div className="flex items-center gap-2">
        <Input
          type="number"
          min={0}
          step="0.01"
          value={shown || ""}
          onChange={(e) => {
            onPaidInFull(false);
            onTyped(Number(e.target.value) || 0);
          }}
          aria-label="Amount paid"
        />
        <Button
          type="button"
          variant={paidInFull ? "primary" : "outline"}
          aria-pressed={paidInFull}
          disabled={!(total > 0)}
          onClick={() => {
            // Turning it off clears the field, back to credit, rather than
            // leaving a typed-looking number nobody typed.
            if (paidInFull) onTyped(0);
            onPaidInFull(!paidInFull);
          }}
          className="shrink-0"
        >
          {paidInFull && <Icon name="check" size={16} />}
          Paid in full
        </Button>
      </div>
    </Field>
  );
}
