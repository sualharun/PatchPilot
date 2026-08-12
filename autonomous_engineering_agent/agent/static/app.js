const successfulStatuses = new Set(["success", "pr_opened"]);
const runningStatuses = new Set(["queued", "running"]);

function statusMatches(actual, selected) {
  if (!selected) return true;
  if (selected === "success") return successfulStatuses.has(actual);
  if (selected === "running") return runningStatuses.has(actual);
  if (selected === "failed") {
    return !successfulStatuses.has(actual) && !runningStatuses.has(actual);
  }
  return actual === selected;
}

function initRunFilters() {
  const body = document.querySelector("#runs-table-body");
  const search = document.querySelector("#runs-search");
  if (!body || !search) return;

  const rows = Array.from(body.querySelectorAll("tr[data-status]"));
  const searchControl = document.querySelector("#runs-search-control");
  const searchField = document.querySelector("#runs-search-field");
  const clearSearch = document.querySelector("#runs-search-clear");
  const suggestions = document.querySelector("#runs-search-suggestions");
  const repository = document.querySelector("#runs-repository");
  const status = document.querySelector("#runs-status");
  const model = document.querySelector("#runs-model");
  const range = document.querySelector("#runs-range");
  const reset = document.querySelector("#runs-reset");
  const count = document.querySelector("#runs-result-count");
  const emptyState = body.querySelector("tr[data-empty-state]");
  const noMatches = document.createElement("tr");
  noMatches.dataset.filterEmpty = "true";
  noMatches.hidden = true;
  noMatches.innerHTML = '<td colspan="10">No runs match these filters.</td>';
  body.append(noMatches);

  const searchPlaceholders = {
    all: "Search all run fields",
    run: "Search run IDs",
    repository: "Search repositories",
    issue: "Search issue numbers or URLs",
    branch: "Search branches",
  };

  function setSuggestionsOpen(open) {
    if (!suggestions) return;
    suggestions.hidden = !open;
    search.setAttribute("aria-expanded", String(open));
  }

  function searchableValue(row) {
    const selectedField = searchField?.value || "all";
    if (selectedField === "run") return row.dataset.runId || "";
    if (selectedField === "repository") return row.dataset.repository || "";
    if (selectedField === "issue") return row.dataset.issue || "";
    if (selectedField === "branch") return row.dataset.branch || "";
    return row.dataset.searchAll || row.textContent;
  }

  function updateSearchPlaceholder() {
    const selectedField = searchField?.value || "all";
    search.placeholder = searchPlaceholders[selectedField] || searchPlaceholders.all;
  }

  function renderSuggestions(matchedRows, query) {
    if (!suggestions || !query) {
      setSuggestionsOpen(false);
      return;
    }

    suggestions.replaceChildren();
    if (matchedRows.length === 0) {
      const empty = document.createElement("p");
      empty.className = "runs-search-empty";
      empty.textContent = "No matching runs";
      suggestions.append(empty);
      setSuggestionsOpen(true);
      return;
    }

    for (const row of matchedRows.slice(0, 5)) {
      const link = document.createElement("a");
      link.href = row.dataset.runHref || "/runs";
      link.setAttribute("role", "option");

      const identity = document.createElement("span");
      const runId = document.createElement("strong");
      runId.textContent = row.dataset.runId || "Run";
      const repositoryName = document.createElement("small");
      repositoryName.textContent = row.dataset.repository || "Unknown repository";
      identity.append(runId, repositoryName);

      const context = document.createElement("span");
      context.className = "runs-search-context";
      context.textContent = `Issue ${row.dataset.issue?.split(" ")[0] || "–"}`;

      link.append(identity, context);
      suggestions.append(link);
    }
    setSuggestionsOpen(true);
  }

  function withinRange(row) {
    const selected = range?.value || "all";
    if (selected === "all") return true;
    const timestamp = Date.parse(row.dataset.startedAt || "");
    if (Number.isNaN(timestamp)) return true;
    const cutoff = Date.now() - Number(selected) * 24 * 60 * 60 * 1000;
    return timestamp >= cutoff;
  }

  function applyFilters() {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    const matchedRows = [];

    for (const row of rows) {
      const matches =
        (!query || searchableValue(row).toLowerCase().includes(query)) &&
        (!repository?.value || row.dataset.repository === repository.value) &&
        statusMatches(row.dataset.status || "", status?.value || "") &&
        (!model?.value || row.dataset.model === model.value) &&
        withinRange(row);
      row.hidden = !matches;
      if (matches) {
        visible += 1;
        matchedRows.push(row);
      }
    }

    if (emptyState) emptyState.hidden = rows.length > 0;
    noMatches.hidden = rows.length === 0 || visible > 0;
    if (count) count.textContent = `Showing ${visible} run${visible === 1 ? "" : "s"}`;
    if (clearSearch) clearSearch.hidden = query.length === 0;
    renderSuggestions(matchedRows, query);
  }

  const requestedStatus = new URLSearchParams(window.location.search).get("status");
  if (requestedStatus && status) status.value = requestedStatus;

  search.addEventListener("input", applyFilters);
  search.addEventListener("focus", applyFilters);
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setSuggestionsOpen(false);
      search.blur();
    }
    if (event.key === "ArrowDown" && !suggestions?.hidden) {
      event.preventDefault();
      suggestions?.querySelector("a")?.focus();
    }
  });
  searchField?.addEventListener("change", () => {
    updateSearchPlaceholder();
    applyFilters();
    search.focus();
  });
  clearSearch?.addEventListener("click", () => {
    search.value = "";
    applyFilters();
    search.focus();
  });
  [repository, status, model, range].forEach((control) => {
    control?.addEventListener("change", applyFilters);
  });
  document.addEventListener("click", (event) => {
    if (searchControl && !searchControl.contains(event.target)) setSuggestionsOpen(false);
  });
  reset?.addEventListener("click", () => {
    search.value = "";
    if (searchField) searchField.value = "all";
    if (repository) repository.value = "";
    if (status) status.value = "";
    if (model) model.value = "";
    if (range) range.value = "7";
    updateSearchPlaceholder();
    window.history.replaceState(null, "", "/runs#run-history");
    const historyTab = document.querySelector('.platform-tabs a[data-run-status=""]');
    document.querySelectorAll(".platform-tabs a").forEach((link) => {
      const isActive = link === historyTab;
      link.classList.toggle("active", isActive);
      if (isActive) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    applyFilters();
  });

  updateSearchPlaceholder();
  applyFilters();
}

function initSectionTabs() {
  const nav = document.querySelector(".platform-tabs");
  if (!nav) return;
  const links = Array.from(nav.querySelectorAll("a"));

  function activate(selected) {
    for (const link of links) {
      const isActive = link === selected;
      link.classList.toggle("active", isActive);
      if (isActive) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  }

  function focusSection(selector) {
    const section = selector ? document.querySelector(selector) : null;
    if (!section) return;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
    section.classList.remove("section-focus");
    window.requestAnimationFrame(() => section.classList.add("section-focus"));
    window.setTimeout(() => section.classList.remove("section-focus"), 900);
  }

  for (const link of links) {
    link.addEventListener("click", (event) => {
      const destination = new URL(link.href, window.location.href);
      if (destination.pathname !== window.location.pathname || !link.dataset.section) return;

      const section = document.querySelector(link.dataset.section);
      if (!section) return;
      event.preventDefault();

      if (Object.hasOwn(link.dataset, "runStatus")) {
        const status = document.querySelector("#runs-status");
        if (status) {
          status.value = link.dataset.runStatus || "";
          status.dispatchEvent(new Event("change", { bubbles: true }));
        }
        if (link.dataset.runStatus) destination.searchParams.set("status", link.dataset.runStatus);
        else destination.searchParams.delete("status");
      }

      window.history.replaceState(
        null,
        "",
        `${destination.pathname}${destination.search}${destination.hash}`,
      );
      activate(link);
      focusSection(link.dataset.section);
    });
  }

  const requestedStatus = new URLSearchParams(window.location.search).get("status");
  const current = links.find((link) => {
    if (requestedStatus === "failed") return link.dataset.runStatus === "failed";
    return link.dataset.section && link.dataset.section === window.location.hash;
  });
  if (current) activate(current);
}

const TOUR_STORAGE_KEY = "patchpilot.tour.dismissed";

// Ordered by importance: the first two steps that resolve on the page are shown.
const TOUR_STEPS = [
  {
    selector: "#runs-table-body, #recent-runs",
    title: "Your runs live here",
    body: "Every run records its commands, test output, patch, and cost. Open one to follow the agent step by step.",
  },
  {
    selector: "[data-tour='start-run'], #start-run",
    title: "Start a run",
    body: "Paste a GitHub issue URL, pick a model, and PatchPilot plans a fix and runs your tests in a sandbox.",
  },
  {
    selector: "[data-tour='settings'], a[href='/settings#providers']",
    title: "Configure your API key",
    body: "Settings holds your provider keys, GitHub App status, and run policy. Test a key before your first run.",
  },
];

function tourDismissed() {
  try {
    return window.localStorage.getItem(TOUR_STORAGE_KEY) === "1";
  } catch (error) {
    return true; // Storage blocked: never nag on every page load.
  }
}

function dismissTour() {
  try {
    window.localStorage.setItem(TOUR_STORAGE_KEY, "1");
  } catch (error) {
    /* storage unavailable; the tour simply reappears next visit */
  }
}

function initTour() {
  if (tourDismissed()) return;
  const steps = TOUR_STEPS.map((step) => ({
    ...step,
    target: document.querySelector(step.selector),
  })).filter((step) => step.target);
  if (!steps.length) return;

  let index = 0;
  const bubble = document.createElement("div");
  bubble.className = "tour-bubble";
  bubble.setAttribute("role", "dialog");
  bubble.setAttribute("aria-label", "Product tour");
  document.body.appendChild(bubble);

  function close() {
    dismissTour();
    bubble.remove();
    steps.forEach((step) => step.target.classList.remove("tour-highlight"));
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place);
  }

  function place() {
    const { target } = steps[index];
    const box = target.getBoundingClientRect();
    const top = window.scrollY + box.bottom + 12;
    const left = Math.max(12, Math.min(window.scrollX + box.left, window.scrollX + window.innerWidth - 340));
    bubble.style.top = `${top}px`;
    bubble.style.left = `${left}px`;
  }

  function render() {
    const step = steps[index];
    steps.forEach((other) => other.target.classList.remove("tour-highlight"));
    step.target.classList.add("tour-highlight");
    bubble.innerHTML = "";

    const heading = document.createElement("h3");
    heading.textContent = step.title;
    const body = document.createElement("p");
    body.textContent = step.body;
    const footer = document.createElement("div");
    footer.className = "tour-actions";

    const progress = document.createElement("small");
    progress.textContent = `${index + 1} of ${steps.length}`;
    const skip = document.createElement("button");
    skip.type = "button";
    skip.className = "button outline";
    skip.textContent = "Skip";
    skip.addEventListener("click", close);
    const next = document.createElement("button");
    next.type = "button";
    next.className = "button primary";
    next.textContent = index === steps.length - 1 ? "Done" : "Next";
    next.addEventListener("click", () => {
      if (index === steps.length - 1) {
        close();
        return;
      }
      index += 1;
      render();
    });

    footer.append(progress, skip, next);
    bubble.append(heading, body, footer);
    place();
    step.target.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  window.addEventListener("resize", place);
  window.addEventListener("scroll", place, { passive: true });
  render();
}

function initVideoEmbeds() {
  for (const embed of document.querySelectorAll("[data-video-embed]")) {
    const video = embed.querySelector("video");
    const button = embed.querySelector(".video-play");
    if (!video || !button) continue;

    button.addEventListener("click", () => {
      // Controls stay off until playback starts so the poster reads as a
      // single clean image rather than a browser chrome sandwich.
      video.controls = true;
      embed.classList.add("is-playing");
      video.play().catch(() => {
        // Autoplay policy or a decode error: leave the controls up so the
        // viewer can start it themselves rather than staring at a dead poster.
        embed.classList.remove("is-playing");
      });
    });

    video.addEventListener("ended", () => {
      video.controls = false;
      video.currentTime = 0;
      embed.classList.remove("is-playing");
    });
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initRunFilters();
  initSectionTabs();
  initTour();
  initVideoEmbeds();
});
