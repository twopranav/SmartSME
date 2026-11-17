/** One of each, for a count of exactly one. */
const SINGULAR: Record<string, string> = {
  sales: "sale",
  purchases: "purchase",
  expenses: "expense",
  products: "product",
  parties: "party",
};

/**
 * "12 sales, 3 purchases and 40 products": what goes with a business when it
 * is deleted, leaving out whatever there is none of. Empty when there is
 * nothing to lose.
 */
export function describeRecords(records: Record<string, number>): string {
  const parts = Object.entries(records)
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${count} ${count === 1 ? (SINGULAR[label] ?? label) : label}`);
  if (parts.length <= 1) return parts.join("");
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}
