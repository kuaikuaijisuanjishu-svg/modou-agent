import {describe, expect, it} from "vitest";
import {
  capabilityAvailable, capabilityTone, normalizePastedToken,
  resolveStartupToken, statusView,
} from "./presentation";

describe("public presentation boundary", () => {
  it("extracts the one-time token from a complete local URL", () => {
    expect(normalizePastedToken("http://127.0.0.1:8765/#token=abc123")).toBe("abc123");
  });

  it("prefers the startup fragment over a stored token", () => {
    expect(resolveStartupToken("#token=fresh", "stored")).toBe("fresh");
  });

  it("renders a complete review as success", () => {
    expect(statusView("COMPLETE").tone).toBe("success");
  });

  it("keeps a disabled capability unavailable", () => {
    expect(capabilityAvailable([{id: "ddmin", state: "disabled", state_label: "已关闭",
      runtime: "unavailable", title: "ddmin", summary: "", gate: "", evidence: []}], "ddmin")).toBe(false);
  });

  it("uses a distinct warning tone for experimental capabilities", () => {
    expect(capabilityTone("experimental")).toBe("warning");
  });
});
