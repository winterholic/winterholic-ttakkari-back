import { afterEach, describe, expect, it, vi } from "vitest";
import { copyText } from "./clipboard";

afterEach(() => {
  vi.restoreAllMocks();
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
});

describe("copyText", () => {
  it("Clipboard API가 있으면 그것으로 복사한다", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    expect(await copyText("본문")).toBe(true);
    expect(writeText).toHaveBeenCalledWith("본문");
  });

  it("Clipboard API가 거절하면 execCommand 대체 경로를 쓰고, 둘 다 안 되면 false다", async () => {
    Object.defineProperty(navigator, "clipboard", { value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) }, configurable: true });
    const exec = vi.fn().mockReturnValue(true);
    (document as unknown as { execCommand: unknown }).execCommand = exec;
    expect(await copyText("본문")).toBe(true);
    expect(exec).toHaveBeenCalledWith("copy");
    exec.mockReturnValue(false);
    expect(await copyText("본문")).toBe(false);
    expect(document.querySelector("textarea")).toBeNull();
  });
});
