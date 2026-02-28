/**
 * Tests for api.js utility functions.
 */
import { describe, it, expect } from "vitest";
import { formatSize, formatDate } from "../api";

describe("formatSize", () => {
  it("formats 0 bytes", () => {
    expect(formatSize(0)).toBe("0 B");
  });

  it("formats null as 0 B", () => {
    expect(formatSize(null)).toBe("0 B");
  });

  it("formats bytes", () => {
    expect(formatSize(512)).toBe("512 B");
  });

  it("formats KB", () => {
    expect(formatSize(1024)).toBe("1.0 KB");
    expect(formatSize(1536)).toBe("1.5 KB");
  });

  it("formats MB", () => {
    expect(formatSize(1048576)).toBe("1.0 MB");
  });

  it("formats GB", () => {
    expect(formatSize(1073741824)).toBe("1.0 GB");
  });

  it("formats TB", () => {
    expect(formatSize(1099511627776)).toBe("1.0 TB");
  });
});

describe("formatDate", () => {
  it("formats null as dash", () => {
    expect(formatDate(null)).toBe("—");
  });

  it("formats empty string as dash", () => {
    expect(formatDate("")).toBe("—");
  });

  it("formats ISO date string", () => {
    const result = formatDate("2024-01-15T10:30:00Z");
    expect(result).toBeTruthy();
    expect(result).not.toBe("—");
  });
});
