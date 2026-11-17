import { describe, expect, it } from "vitest";
import { describeRecords } from "./account";

describe("describeRecords", () => {
  it("lists what a business holds, in order, joined like a sentence", () => {
    expect(describeRecords({ sales: 12, purchases: 3, products: 40 })).toBe(
      "12 sales, 3 purchases and 40 products",
    );
  });

  it("uses the singular for exactly one", () => {
    expect(describeRecords({ sales: 1, parties: 1 })).toBe("1 sale and 1 party");
  });

  it("leaves out what there is none of", () => {
    expect(describeRecords({ sales: 0, purchases: 0, expenses: 2 })).toBe("2 expenses");
  });

  it("is empty for a business with nothing in it", () => {
    expect(describeRecords({ sales: 0, purchases: 0 })).toBe("");
    expect(describeRecords({})).toBe("");
  });
});
