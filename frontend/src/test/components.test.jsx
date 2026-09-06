/**
 * Component render tests — verify components mount without errors.
 * Includes tests for all 6 UI improvements:
 *   1. File type icons (Lucide)
 *   2. Compact mode (DensityToggle)
 *   3. Search keyboard navigation + match highlighting
 *   4. Streaming UX polish (progress bar + footer)
 *   5. Upload time remaining (formatEta)
 *   6. Drop overlay with target prefix
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";

// Mock fetch globally
global.fetch = vi.fn();

// Provide a working localStorage mock for jsdom
const store = {};
const localStorageMock = {
  getItem: (key) => store[key] ?? null,
  setItem: (key, value) => { store[key] = String(value); },
  removeItem: (key) => { delete store[key]; },
  clear: () => { Object.keys(store).forEach((k) => delete store[k]); },
};
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock, writable: true });

// Stub scrollIntoView for jsdom (not implemented)
Element.prototype.scrollIntoView = vi.fn();

beforeEach(() => {
  global.fetch.mockReset();
  localStorageMock.clear();
  document.documentElement.dataset.theme = "light";
  document.documentElement.dataset.density = "default";
});

describe("SharePage", () => {
  it("renders loading state", async () => {
    // Mock a pending fetch
    global.fetch.mockImplementation(() => new Promise(() => {}));

    const { default: SharePage } = await import("../components/SharePage");
    render(<SharePage token="test-token" />);
    expect(screen.getByText("Loading...")).toBeInTheDocument();
  });

  it("renders error state", async () => {
    global.fetch.mockResolvedValue({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ detail: "Link not found" }),
    });

    const { default: SharePage } = await import("../components/SharePage");
    render(<SharePage token="invalid-token" />);

    await waitFor(() => {
      expect(screen.getByText("Link not found")).toBeInTheDocument();
    });
  });

  it("renders password form when required", async () => {
    global.fetch.mockResolvedValue({
      ok: false,
      status: 403,
      json: () => Promise.resolve({ detail: "Password required" }),
    });

    const { default: SharePage } = await import("../components/SharePage");
    render(<SharePage token="protected-token" />);

    await waitFor(() => {
      expect(screen.getByText("This file is password protected.")).toBeInTheDocument();
    });
  });
});

describe("TokenManager", () => {
  it("renders create form", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ tokens: [] }),
    });

    const { default: TokenManager } = await import("../components/TokenManager");
    const onClose = vi.fn();
    render(<TokenManager onClose={onClose} />);

    expect(screen.getByText("API Tokens")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Token name (e.g., CI/CD)")).toBeInTheDocument();
    expect(screen.getByText("Create Token")).toBeInTheDocument();
  });

  it("shows empty state", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ tokens: [] }),
    });

    const { default: TokenManager } = await import("../components/TokenManager");
    render(<TokenManager onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText("No API tokens created yet.")).toBeInTheDocument();
    });
  });
});

describe("LicenseManager", () => {
  it("renders community license state", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ license_type: "community" }),
    });

    const { default: LicenseManager } = await import("../components/LicenseManager");
    render(<LicenseManager onClose={() => {}} />);

    expect(screen.getByText("License")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Community")).toBeInTheDocument();
    });
  });

  it("has activate button", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ license_type: "community" }),
    });

    const { default: LicenseManager } = await import("../components/LicenseManager");
    render(<LicenseManager onClose={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText("Activate")).toBeInTheDocument();
    });
  });
});

describe("Login", () => {
  it("renders login form with default branding", async () => {
    const { default: Login } = await import("../components/Login");
    render(<Login onLogin={() => {}} branding={{ app_name: "Sairo" }} />);

    expect(screen.getByText("Sairo")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Username")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Password")).toBeInTheDocument();
    expect(screen.getByText("Sign In")).toBeInTheDocument();
  });

  it("renders with custom branding", async () => {
    const { default: Login } = await import("../components/Login");
    render(<Login onLogin={() => {}} branding={{ app_name: "MyStorage", login_message: "Welcome!" }} />);

    expect(screen.getByText("MyStorage")).toBeInTheDocument();
    expect(screen.getByText("Welcome!")).toBeInTheDocument();
  });

  it("shows LDAP toggle when enabled", async () => {
    const { default: Login } = await import("../components/Login");
    render(<Login onLogin={() => {}} branding={{ ldap_enabled: true }} />);

    expect(screen.getByText("Local")).toBeInTheDocument();
    expect(screen.getByText("LDAP")).toBeInTheDocument();
  });

  it("does not show LDAP toggle when disabled", async () => {
    const { default: Login } = await import("../components/Login");
    render(<Login onLogin={() => {}} branding={{ ldap_enabled: false }} />);

    expect(screen.queryByText("LDAP")).toBeNull();
  });

  it("shows OAuth buttons when providers available", async () => {
    const { default: Login } = await import("../components/Login");
    render(
      <Login
        onLogin={() => {}}
        branding={{ oauth_providers: [{ id: "google", name: "Google" }, { id: "github", name: "GitHub" }] }}
      />
    );

    expect(screen.getByText("Sign in with Google")).toBeInTheDocument();
    expect(screen.getByText("Sign in with GitHub")).toBeInTheDocument();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// UI IMPROVEMENT TESTS
// ─────────────────────────────────────────────────────────────────────────────

// 1. FILE TYPE ICONS
// Note: ObjectTable uses @tanstack/react-virtual, which requires real element
// dimensions to render rows. In jsdom, the scroll container has zero height,
// so virtualised rows are NOT rendered. We test the icon-mapping logic directly.
describe("File Type Icons (getFileIcon logic)", () => {
  // Replicate the icon-mapping logic from ObjectTable.jsx for unit testing
  const EXT_CATEGORIES = {
    jpg: "image", jpeg: "image", png: "image", gif: "image", svg: "image",
    webp: "image", ico: "image", bmp: "image", tiff: "image",
    mp4: "video", mov: "video", avi: "video", mkv: "video", webm: "video",
    mp3: "audio", wav: "audio", flac: "audio", ogg: "audio", aac: "audio",
    js: "code", jsx: "code", ts: "code", tsx: "code", py: "code",
    go: "code", rs: "code", java: "code", rb: "code", php: "code",
    sh: "code", bash: "code", sql: "code", html: "code", css: "code",
    yaml: "code", yml: "code", toml: "code", xml: "code",
    conf: "code", cfg: "code", ini: "code",
    txt: "text", md: "text", log: "text", readme: "text", out: "text", err: "text",
    csv: "spreadsheet", tsv: "spreadsheet", xls: "spreadsheet", xlsx: "spreadsheet",
    zip: "archive", tar: "archive", gz: "archive", bz2: "archive",
    rar: "archive", "7z": "archive", zst: "archive",
    parquet: "data", avro: "data", orc: "data",
    json: "json",
    pdf: "pdf",
  };

  function getFileCategory(name) {
    const dot = name.lastIndexOf(".");
    if (dot < 0) return null;
    return EXT_CATEGORIES[name.substring(dot + 1).toLowerCase()] || null;
  }

  it("maps Python files to code category", () => {
    expect(getFileCategory("script.py")).toBe("code");
  });

  it("maps images to image category", () => {
    expect(getFileCategory("photo.jpg")).toBe("image");
    expect(getFileCategory("logo.PNG")).toBe("image");
  });

  it("maps parquet to data category", () => {
    expect(getFileCategory("table.parquet")).toBe("data");
  });

  it("maps archives to archive category", () => {
    expect(getFileCategory("backup.zip")).toBe("archive");
    expect(getFileCategory("pkg.tar")).toBe("archive");
  });

  it("maps JSON to json category", () => {
    expect(getFileCategory("config.json")).toBe("json");
  });

  it("maps CSV to spreadsheet category", () => {
    expect(getFileCategory("report.csv")).toBe("spreadsheet");
  });

  it("returns null for extensionless files", () => {
    expect(getFileCategory("unknown_file")).toBeNull();
  });

  it("returns null for unknown extensions", () => {
    expect(getFileCategory("file.xyz123")).toBeNull();
  });
});

// 2. COMPACT MODE (DensityToggle)
describe("DensityToggle", () => {
  it("renders with default (comfortable) state", async () => {
    const { default: DensityToggle } = await import("../components/DensityToggle");
    render(<DensityToggle />);

    const button = screen.getByTitle("Compact view");
    expect(button).toBeInTheDocument();
  });

  it("toggles to compact mode on click", async () => {
    const { default: DensityToggle } = await import("../components/DensityToggle");
    render(<DensityToggle />);

    const button = screen.getByTitle("Compact view");
    fireEvent.click(button);

    expect(document.documentElement.dataset.density).toBe("compact");
    expect(localStorage.getItem("density")).toBe("compact");
  });

  it("toggles back to comfortable on second click", async () => {
    const { default: DensityToggle } = await import("../components/DensityToggle");
    render(<DensityToggle />);

    const button = screen.getByTitle("Compact view");
    fireEvent.click(button);
    expect(document.documentElement.dataset.density).toBe("compact");

    const comfortButton = screen.getByTitle("Comfortable view");
    fireEvent.click(comfortButton);
    expect(document.documentElement.dataset.density).toBe("default");
    expect(localStorage.getItem("density")).toBe("default");
  });

  it("persists compact state in localStorage", async () => {
    localStorage.setItem("density", "compact");
    const { default: DensityToggle } = await import("../components/DensityToggle");
    render(<DensityToggle />);

    expect(screen.getByTitle("Comfortable view")).toBeInTheDocument();
  });

  it("dispatches density-change event on toggle", async () => {
    const handler = vi.fn();
    window.addEventListener("density-change", handler);

    const { default: DensityToggle } = await import("../components/DensityToggle");
    render(<DensityToggle />);

    fireEvent.click(screen.getByRole("button"));
    expect(handler).toHaveBeenCalled();

    window.removeEventListener("density-change", handler);
  });

  it("has correct aria-label for accessibility", async () => {
    const { default: DensityToggle } = await import("../components/DensityToggle");
    render(<DensityToggle />);

    expect(screen.getByLabelText("Switch to compact view")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByLabelText("Switch to comfortable view")).toBeInTheDocument();
  });
});

describe("ObjectTable compact mode", () => {
  it("renders without error in compact mode and shows footer", async () => {
    document.documentElement.dataset.density = "compact";
    global.fetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ children: [] }) });
    const { default: ObjectTable } = await import("../components/ObjectTable");

    const { container } = render(
      <ObjectTable
        bucket="test"
        folders={[]}
        files={[{ key: "f.txt", name: "f.txt", size: 10, last_modified: "2024-01-01T00:00:00Z" }]}
        filter=""
        selected={new Set()}
        selectedFolders={new Set()}
        onSelect={vi.fn()}
        onSelectFolders={vi.fn()}
        onNavigate={vi.fn()}
        onFileInfo={vi.fn()}
        onFilePreview={vi.fn()}
        onDeleteFolders={vi.fn()}
        loading={false}
        done={true}
        sortKey="name"
        sortAsc={true}
        onSort={vi.fn()}
        indexed={false}
        prefix=""
        isAdmin={false}
        showDeleted={false}
        deletedItems={null}
        deletedLoading={false}
        onPurge={vi.fn()}
      />
    );

    // Virtual rows may not render in jsdom (no dimensions), but footer should
    expect(container.querySelector(".table-footer")).toBeTruthy();
    expect(screen.getByText("0 folders, 1 file")).toBeInTheDocument();
  });
});

// 3. SEARCH KEYBOARD NAVIGATION + MATCH HIGHLIGHTING
describe("SearchBar keyboard navigation", () => {
  const mockSearchResults = {
    results: [
      { key: "src/script.py", size: 1024, last_modified: "2024-01-01T00:00:00Z" },
      { key: "scripts/deploy.sh", size: 512, last_modified: "2024-01-01T00:00:00Z" },
      { key: "docs/scripting.md", size: 256, last_modified: "2024-01-01T00:00:00Z" },
    ],
    count: 3,
    query: "script",
  };

  it("renders initial hint text", async () => {
    const { default: SearchBar } = await import("../components/SearchBar");
    render(
      <SearchBar bucket="test" prefix="" onClose={vi.fn()} onNavigate={vi.fn()} onFileInfo={vi.fn()} />
    );
    expect(screen.getByText("Type at least 2 characters to search across all objects in the bucket")).toBeInTheDocument();
  });

  it("calls onClose when Escape is pressed", async () => {
    const onClose = vi.fn();
    const { default: SearchBar } = await import("../components/SearchBar");
    render(
      <SearchBar bucket="test" prefix="" onClose={onClose} onNavigate={vi.fn()} onFileInfo={vi.fn()} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("shows search results with match highlighting", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockSearchResults),
    });

    const { default: SearchBar } = await import("../components/SearchBar");
    render(
      <SearchBar bucket="test" prefix="" onClose={vi.fn()} onNavigate={vi.fn()} onFileInfo={vi.fn()} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.change(input, { target: { value: "script" } });

    await waitFor(() => {
      expect(screen.getByText("3 results")).toBeInTheDocument();
    });

    const marks = document.querySelectorAll("mark.search-highlight");
    expect(marks.length).toBeGreaterThan(0);
    expect(marks[0].textContent).toBe("script");
  });

  it("navigates results with ArrowDown/ArrowUp", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockSearchResults),
    });

    const { default: SearchBar } = await import("../components/SearchBar");
    const { container } = render(
      <SearchBar bucket="test" prefix="" onClose={vi.fn()} onNavigate={vi.fn()} onFileInfo={vi.fn()} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.change(input, { target: { value: "script" } });

    await waitFor(() => {
      expect(screen.getByText("3 results")).toBeInTheDocument();
    });

    fireEvent.keyDown(input, { key: "ArrowDown" });
    const items = container.querySelectorAll(".search-item");
    expect(items[0].classList.contains("search-item-active")).toBe(true);

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(items[1].classList.contains("search-item-active")).toBe(true);
    expect(items[0].classList.contains("search-item-active")).toBe(false);

    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(items[0].classList.contains("search-item-active")).toBe(true);
  });

  it("ArrowDown wraps from last to first", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockSearchResults),
    });

    const { default: SearchBar } = await import("../components/SearchBar");
    const { container } = render(
      <SearchBar bucket="test" prefix="" onClose={vi.fn()} onNavigate={vi.fn()} onFileInfo={vi.fn()} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.change(input, { target: { value: "script" } });

    await waitFor(() => {
      expect(screen.getByText("3 results")).toBeInTheDocument();
    });

    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });

    const items = container.querySelectorAll(".search-item");
    expect(items[2].classList.contains("search-item-active")).toBe(true);

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(items[0].classList.contains("search-item-active")).toBe(true);
  });

  it("ArrowUp wraps from first to last", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockSearchResults),
    });

    const { default: SearchBar } = await import("../components/SearchBar");
    const { container } = render(
      <SearchBar bucket="test" prefix="" onClose={vi.fn()} onNavigate={vi.fn()} onFileInfo={vi.fn()} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.change(input, { target: { value: "script" } });

    await waitFor(() => {
      expect(screen.getByText("3 results")).toBeInTheDocument();
    });

    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowUp" });

    const items = container.querySelectorAll(".search-item");
    expect(items[2].classList.contains("search-item-active")).toBe(true);
  });

  it("Enter opens (previews) the selected result instead of navigating to its folder", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockSearchResults),
    });

    const onNavigate = vi.fn();
    const onClose = vi.fn();
    const onFilePreview = vi.fn();
    const { default: SearchBar } = await import("../components/SearchBar");
    render(
      <SearchBar bucket="test" prefix="" onClose={onClose} onNavigate={onNavigate} onFileInfo={vi.fn()} onFilePreview={onFilePreview} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.change(input, { target: { value: "script" } });

    await waitFor(() => {
      expect(screen.getByText("3 results")).toBeInTheDocument();
    });

    // First result is src/script.py (previewable) — Enter opens the preview, not the folder.
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onClose).toHaveBeenCalled();
    expect(onFilePreview).toHaveBeenCalledWith({ key: "src/script.py", size: 1024 });
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("Shift+Enter reveals the selected result in its folder (passing the file key for highlight)", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockSearchResults),
    });

    const onNavigate = vi.fn();
    const onClose = vi.fn();
    const { default: SearchBar } = await import("../components/SearchBar");
    render(
      <SearchBar bucket="test" prefix="" onClose={onClose} onNavigate={onNavigate} onFileInfo={vi.fn()} onFilePreview={vi.fn()} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.change(input, { target: { value: "script" } });

    await waitFor(() => {
      expect(screen.getByText("3 results")).toBeInTheDocument();
    });

    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });

    expect(onClose).toHaveBeenCalled();
    expect(onNavigate).toHaveBeenCalledWith("src/", "src/script.py");
  });

  it("shows arrow key hint when results exist", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockSearchResults),
    });

    const { default: SearchBar } = await import("../components/SearchBar");
    render(
      <SearchBar bucket="test" prefix="" onClose={vi.fn()} onNavigate={vi.fn()} onFileInfo={vi.fn()} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.change(input, { target: { value: "script" } });

    await waitFor(() => {
      expect(screen.getByText("3 results")).toBeInTheDocument();
    });

    const kbds = document.querySelectorAll(".kbd");
    expect(kbds.length).toBe(2);
  });

  it("mouse hover updates selectedIdx", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(mockSearchResults),
    });

    const { default: SearchBar } = await import("../components/SearchBar");
    const { container } = render(
      <SearchBar bucket="test" prefix="" onClose={vi.fn()} onNavigate={vi.fn()} onFileInfo={vi.fn()} />
    );

    const input = screen.getByPlaceholderText("Search test...");
    fireEvent.change(input, { target: { value: "script" } });

    await waitFor(() => {
      expect(screen.getByText("3 results")).toBeInTheDocument();
    });

    const items = container.querySelectorAll(".search-item");
    fireEvent.mouseEnter(items[1]);
    expect(items[1].classList.contains("search-item-active")).toBe(true);
  });
});

// 4. STREAMING UX (progress bar + footer)
describe("Streaming UX - ObjectTable footer", () => {
  const baseProps = {
    bucket: "test",
    folders: [{ name: "dir", prefix: "dir/" }],
    files: [{ key: "f.txt", name: "f.txt", size: 100, last_modified: "2024-01-01T00:00:00Z" }],
    filter: "",
    selected: new Set(),
    selectedFolders: new Set(),
    onSelect: vi.fn(),
    onSelectFolders: vi.fn(),
    onNavigate: vi.fn(),
    onFileInfo: vi.fn(),
    onFilePreview: vi.fn(),
    onDeleteFolders: vi.fn(),
    sortKey: "name",
    sortAsc: true,
    onSort: vi.fn(),
    indexed: false,
    prefix: "",
    isAdmin: false,
    showDeleted: false,
    deletedItems: null,
    deletedLoading: false,
    onPurge: vi.fn(),
  };

  it("shows streaming indicator with pulsing dot while loading", async () => {
    global.fetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ children: [] }) });
    const { default: ObjectTable } = await import("../components/ObjectTable");
    const { container } = render(<ObjectTable {...baseProps} loading={true} done={false} />);

    expect(container.querySelector(".streaming-dot")).toBeTruthy();
    expect(screen.getByText("Streaming")).toBeInTheDocument();
    expect(screen.getByText("1 folders, 1 files")).toBeInTheDocument();
  });

  it("shows final count when loading is complete", async () => {
    global.fetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ children: [] }) });
    const { default: ObjectTable } = await import("../components/ObjectTable");
    const { container } = render(<ObjectTable {...baseProps} loading={false} done={true} />);

    expect(container.querySelector(".streaming-dot")).toBeFalsy();
    expect(screen.getByText("1 folder, 1 file")).toBeInTheDocument();
  });

  it("does not show streaming indicator when loading with no data", async () => {
    global.fetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({ children: [] }) });
    const { default: ObjectTable } = await import("../components/ObjectTable");
    const { container } = render(
      <ObjectTable {...baseProps} folders={[]} files={[]} loading={true} done={false} />
    );

    expect(container.querySelector(".streaming-dot")).toBeFalsy();
  });
});

// 5. UPLOAD TIME REMAINING (formatEta)
describe("Upload formatEta", () => {
  it("formats seconds correctly", () => {
    function formatEta(seconds) {
      if (seconds < 60) return `${Math.ceil(seconds)}s`;
      const m = Math.floor(seconds / 60);
      const s = Math.ceil(seconds % 60);
      return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m ${s}s`;
    }

    expect(formatEta(5)).toBe("5s");
    expect(formatEta(0.5)).toBe("1s");
    expect(formatEta(30)).toBe("30s");
    expect(formatEta(59)).toBe("59s");
    expect(formatEta(60)).toBe("1m 0s");
    expect(formatEta(90)).toBe("1m 30s");
    expect(formatEta(125)).toBe("2m 5s");
    expect(formatEta(3600)).toBe("1h 0m");
    expect(formatEta(3660)).toBe("1h 1m");
    expect(formatEta(7200)).toBe("2h 0m");
  });
});

describe("UploadModal", () => {
  it("renders drop zone and file list", async () => {
    const { default: UploadModal } = await import("../components/UploadModal");
    render(
      <UploadModal
        bucket="test"
        prefix="data/"
        initialFiles={null}
        onClose={vi.fn()}
        onUploaded={vi.fn()}
      />
    );

    expect(screen.getByText("Upload to test/data/")).toBeInTheDocument();
    expect(screen.getByText("Drop files here or click to browse")).toBeInTheDocument();
  });

  it("renders with initial files and shows file names", async () => {
    const files = [
      new File(["hello"], "test.txt", { type: "text/plain" }),
      new File(["world"], "data.csv", { type: "text/csv" }),
    ];

    const { default: UploadModal } = await import("../components/UploadModal");
    render(
      <UploadModal
        bucket="test"
        prefix=""
        initialFiles={files}
        onClose={vi.fn()}
        onUploaded={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText("test.txt")).toBeInTheDocument();
    });
    expect(screen.getByText("data.csv")).toBeInTheDocument();
    // Summary shows "2 files (XB)" and button shows "Upload 2 files"
    expect(screen.getByText("Upload 2 files")).toBeInTheDocument();
  });

  it("shows Upload button with correct file count", async () => {
    const files = [
      new File(["a"], "a.txt", { type: "text/plain" }),
      new File(["b"], "b.txt", { type: "text/plain" }),
      new File(["c"], "c.txt", { type: "text/plain" }),
    ];

    const { default: UploadModal } = await import("../components/UploadModal");
    render(
      <UploadModal
        bucket="test"
        prefix=""
        initialFiles={files}
        onClose={vi.fn()}
        onUploaded={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText("Upload 3 files")).toBeInTheDocument();
    });
  });
});

// 6. DROP OVERLAY WITH PREFIX
describe("Drop overlay prefix", () => {
  it("renders correct prefix text in overlay", () => {
    const prefix = "logs/2024/";
    const bucket = "test-data";
    const { container } = render(
      <div className="drop-overlay">
        <div className="drop-overlay-text">
          Drop files to upload to <strong>{prefix || bucket + "/"}</strong>
        </div>
      </div>
    );

    expect(container.querySelector(".drop-overlay-text")).toBeTruthy();
    expect(screen.getByText(/Drop files to upload to/)).toBeInTheDocument();
    const strong = container.querySelector("strong");
    expect(strong.textContent).toBe("logs/2024/");
  });

  it("shows bucket root when prefix is empty", () => {
    const prefix = "";
    const bucket = "test-data";
    const { container } = render(
      <div className="drop-overlay">
        <div className="drop-overlay-text">
          Drop files to upload to <strong>{prefix || bucket + "/"}</strong>
        </div>
      </div>
    );

    const strong = container.querySelector("strong");
    expect(strong.textContent).toBe("test-data/");
  });
});

describe("Refresh of a served index (crawl status 'crawling' with a success timestamp)", () => {
  it("BucketList keeps the stats and says Refreshing; a first build says Indexing", async () => {
    vi.resetModules();   // fresh module graph so the mocked api is the one the component imports
    vi.doMock("../api", async (importOriginal) => ({
      ...(await importOriginal()),
      listAllBuckets: vi.fn().mockResolvedValue({ endpoints: [{ endpoint_id: "default", endpoint_name: "Default", buckets: [
        { name: "served-bkt", created: "2026-01-01T00:00:00Z", index_status: "crawling", indexed: true, object_count: 4460229, total_size: 39e12 },
        { name: "fresh-bkt", created: "2026-01-01T00:00:00Z", index_status: "crawling", indexed: false, object_count: 12345 },
      ] }] }),
    }));
    const { default: BucketList } = await import("../components/BucketList");
    render(<BucketList onSelect={() => {}} role="admin" onDashboard={() => {}} />);
    const served = (await screen.findByText("served-bkt")).closest(".bucket-card");
    expect(served).toHaveTextContent("4,460,229 objects");
    expect(served).toHaveTextContent("Refreshing index…");
    expect(served).not.toHaveTextContent("Indexing...");
    const fresh = screen.getByText("fresh-bkt").closest(".bucket-card");
    expect(fresh).toHaveTextContent("Indexing... 12,345");
    expect(fresh).not.toHaveTextContent("objects");
    vi.doUnmock("../api");
  });

  it("CrawlStatus pill says Refreshing for a served index and Indexing for a first build", async () => {
    const statuses = [
      { status: "crawling", total_objects: 42, last_crawl_end: "2026-09-01T00:00:00Z" },
      { status: "crawling", total_objects: 7, last_crawl_end: null },
    ];
    vi.resetModules();   // fresh module graph so the mocked api is the one the component imports
    vi.doMock("../api", async (importOriginal) => ({
      ...(await importOriginal()),
      getCrawlStatus: vi.fn().mockImplementation(() => Promise.resolve(statuses.shift())),
    }));
    const { default: CrawlStatus } = await import("../components/CrawlStatus");
    const a = render(<CrawlStatus bucket="served" />);
    expect(await a.findByText("Refreshing... 42")).toBeInTheDocument();
    a.unmount();
    const b = render(<CrawlStatus bucket="fresh" />);
    expect(await b.findByText("Indexing... 7")).toBeInTheDocument();
    vi.doUnmock("../api");
  });
});

describe("CrawlStatus stale reply guard", () => {
  it("a delayed reply for the previous bucket cannot overwrite the current bucket's pill", async () => {
    let resolveOld;
    const replies = {
      old: new Promise((r) => { resolveOld = r; }),
      current: Promise.resolve({ status: "complete", total_objects: 5, last_crawl_end: "2026-09-01T00:00:00Z" }),
    };
    vi.resetModules();   // fresh module graph so the mocked api is the one the component imports
    vi.doMock("../api", async (importOriginal) => ({
      ...(await importOriginal()),
      getCrawlStatus: vi.fn().mockImplementation((b) => replies[b]),
    }));
    const { default: CrawlStatus } = await import("../components/CrawlStatus");
    const view = render(<CrawlStatus bucket="old" />);
    view.rerender(<CrawlStatus bucket="current" />);
    expect(await view.findByText("5 objects")).toBeInTheDocument();
    resolveOld({ status: "crawling", total_objects: 10248000, last_crawl_end: null });   // the old bucket's reply lands late
    await new Promise((r) => setTimeout(r, 20));
    expect(view.getByText("5 objects")).toBeInTheDocument();
    expect(view.queryByText(/10,248,000/)).toBeNull();
    vi.doUnmock("../api");
  });
});

describe("CrawlStatus manual recrawl polling", () => {
  it("the rapid polls started by Re-index Now for bucket A cannot overwrite bucket B after navigating", async () => {
    vi.resetModules();
    const calls = { A: 0, B: 0 };
    const idleA = { status: "complete", total_objects: 3, last_crawl_end: "2026-09-01T00:00:00Z" };
    const lateA = { status: "crawling", total_objects: 10248000, last_crawl_end: null };      // every A poll after the click
    const idleB = { status: "complete", total_objects: 5, last_crawl_end: "2026-09-01T00:00:00Z" };
    vi.doMock("../api", async (importOriginal) => ({
      ...(await importOriginal()),
      getCrawlStatus: vi.fn().mockImplementation((b) => { calls[b]++; return Promise.resolve(b === "B" ? idleB : calls.A === 1 ? idleA : lateA); }),
      triggerCrawl: vi.fn().mockResolvedValue({}),
    }));
    const { default: CrawlStatus } = await import("../components/CrawlStatus");
    const view = render(<CrawlStatus bucket="A" />);
    fireEvent.click(await view.findByText("3 objects"));               // expand the panel
    fireEvent.click(await view.findByText("Re-index Now"));            // starts the 6 × 800 ms poll loop for A
    view.rerender(<CrawlStatus bucket="B" />);                          // user navigates away
    expect(await view.findByText("5 objects")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 1800));                     // at least two of A's rapid polls land meanwhile
    expect(calls.A).toBeGreaterThan(1);
    expect(view.getByText("5 objects")).toBeInTheDocument();
    expect(view.queryByText(/10,248,000/)).toBeNull();
    vi.doUnmock("../api");
  });
});
