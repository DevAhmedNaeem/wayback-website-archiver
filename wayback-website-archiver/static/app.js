/**
 * Wayback Website Archiver — Frontend Application Logic
 */

let currentTaskId = null;
let currentJobId = null;
let discoveryPollInterval = null;
let archivePollInterval = null;

let discoveredUrlsList = [];
let selectedUrlsSet = new Set();
let activeJobStatus = null;

// Initial setup
document.addEventListener("DOMContentLoaded", () => {
  checkApiCredentials();
  checkForUnfinishedJob();
  
  // Check if URL has ?resume=<job_id>
  const params = new URLSearchParams(window.location.search);
  const resumeId = params.get("resume");
  if (resumeId) {
    resumeArchivingJob(resumeId);
  }
});

/**
 * Check if IA credentials are configured and valid.
 */
async function checkApiCredentials() {
  const badge = document.getElementById("authBadge");
  try {
    const res = await fetch("/api/settings");
    if (res.ok) {
      const data = await res.json();
      if (data.access_key && data.secret_key) {
        badge.className = "nav-auth-badge auth-badge-ready";
        badge.innerHTML = "<span>✓ IA Connected</span>";
      } else {
        badge.className = "nav-auth-badge auth-badge-warning";
        badge.innerHTML = "<span>⚠ IA Keys Missing</span>";
      }
    }
  } catch (err) {
    badge.className = "nav-auth-badge auth-badge-warning";
    badge.innerHTML = "<span>Offline</span>";
  }
}

/**
 * Check if there is an unfinished job that can be resumed.
 */
async function checkForUnfinishedJob() {
  try {
    const res = await fetch("/api/jobs/unfinished");
    if (res.ok) {
      const data = await res.json();
      if (data && data.job) {
        const job = data.job;
        const banner = document.getElementById("resumeBanner");
        const details = document.getElementById("resumeDetails");
        details.textContent = `Found unfinished job for ${job.website_url}: ${job.current_index} of ${job.urls_to_archive.length} pages captured.`;
        banner.style.display = "flex";
        
        document.getElementById("btnResumeJob").onclick = () => {
          resumeArchivingJob(job.id);
        };
        document.getElementById("btnDismissResume").onclick = () => {
          banner.style.display = "none";
        };
      }
    }
  } catch (e) {
    console.debug("Unfinished job check:", e);
  }
}


/**
 * Start website discovery in specified mode.
 */
async function startDiscovery(mode) {
  const websiteInput = document.getElementById("websiteUrl");
  let url = websiteInput.value.trim();
  if (!url) {
    alert("Please enter a valid website URL.");
    websiteInput.focus();
    return;
  }
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    url = "https://" + url;
    websiteInput.value = url;
  }

  // Read options (safe fallbacks)
  const cfgMaxPagesEl = document.getElementById("cfgMaxPages");
  const maxPages = cfgMaxPagesEl ? (parseInt(cfgMaxPagesEl.value, 10) || 500) : 500;
  const cfgMaxDepthEl = document.getElementById("cfgMaxDepth");
  const maxDepth = cfgMaxDepthEl ? (parseInt(cfgMaxDepthEl.value, 10) || 3) : 3;

  // UI state
  document.getElementById("discoveryProgressSection").style.display = "block";
  document.getElementById("selectionSection").style.display = "none";
  document.getElementById("archivingSection").style.display = "none";
  document.getElementById("completionSection").style.display = "none";
  document.getElementById("discoveryLiveLog").textContent = `Starting ${mode} discovery for ${url}...`;
  document.getElementById("discoveryCountBadge").textContent = "0 Pages Found";

  // Disable buttons while discovering
  const btnMenu = document.getElementById("btnDiscoverMenu");
  const btnSitemap = document.getElementById("btnDiscoverSitemap");
  if (btnMenu) btnMenu.disabled = true;
  if (btnSitemap) btnSitemap.disabled = true;

  try {
    const res = await fetch("/api/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: url,
        mode: mode,
        max_pages: maxPages,
        max_depth: maxDepth,
      }),
    });

    if (!res.ok) {
      throw new Error(await res.text());
    }

    const data = await res.json();
    currentTaskId = data.task_id;

    // Start polling discovery progress
    if (discoveryPollInterval) clearInterval(discoveryPollInterval);
    discoveryPollInterval = setInterval(pollDiscoveryStatus, 1000);
  } catch (err) {
    alert("Failed to start discovery: " + err.message);
    document.getElementById("discoveryProgressSection").style.display = "none";
    if (btnMenu) btnMenu.disabled = false;
    if (btnSitemap) btnSitemap.disabled = false;
  }
}

/**
 * Poll discovery task status.
 */
async function pollDiscoveryStatus() {
  if (!currentTaskId) return;

  try {
    const res = await fetch(`/api/discover/status/${currentTaskId}`);
    if (!res.ok) return;

    const data = await res.json();
    const count = data.count || (data.urls ? data.urls.length : 0);
    document.getElementById("discoveryCountBadge").textContent = `${count} Pages Found`;

    if (data.latest_log) {
      document.getElementById("discoveryLiveLog").textContent = data.latest_log;
    }

    if (data.status === "completed" || data.status === "failed") {
      clearInterval(discoveryPollInterval);
      document.getElementById("discoveryProgressSection").style.display = "none";
      const btnMenu = document.getElementById("btnDiscoverMenu");
      const btnSitemap = document.getElementById("btnDiscoverSitemap");
      if (btnMenu) btnMenu.disabled = false;
      if (btnSitemap) btnSitemap.disabled = false;

      if (data.status === "failed") {
        alert("Discovery failed: " + (data.error || "Unknown error"));
        return;
      }

      // Populate discovered table
      populateDiscoveredUrls(data.website_url, data.urls || []);
    }
  } catch (err) {
    console.error("Poll discovery error:", err);
  }
}

/**
 * Populate discovered pages table with checkboxes.
 */
function populateDiscoveredUrls(websiteUrl, urls) {
  discoveredUrlsList = urls;
  selectedUrlsSet = new Set(urls); // By default, select all

  document.getElementById("lblDiscoveredDomain").textContent = websiteUrl;
  document.getElementById("lblTotalDiscovered").textContent = urls.length;
  document.getElementById("chkMaster").checked = true;

  renderDiscoveredTable(urls);
  updateSelectionCounts();

  document.getElementById("selectionSection").style.display = "block";
  document.getElementById("selectionSection").scrollIntoView({ behavior: "smooth" });
}

function renderDiscoveredTable(urlsToDisplay) {
  const tbody = document.getElementById("discoveredTableBody");
  if (!urlsToDisplay || urlsToDisplay.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 24px;">No pages matched filter.</td></tr>`;
    return;
  }

  tbody.innerHTML = urlsToDisplay.map((url, idx) => {
    const isChecked = selectedUrlsSet.has(url) ? "checked" : "";
    let sourceBadge = "Internal Page";
    if (idx === 0) sourceBadge = "Homepage";
    else if (url.includes("sitemap")) sourceBadge = "Sitemap";
    
    return `
      <tr>
        <td style="text-align: center;">
          <input type="checkbox" class="url-chk" data-url="${escapeHtml(url)}" ${isChecked} onchange="handleUrlCheckboxChange(this)">
        </td>
        <td style="color: var(--text-muted); font-size: 0.8rem;">${idx + 1}</td>
        <td>
          <a href="${escapeHtml(url)}" target="_blank" rel="noopener" style="font-family: monospace; font-size: 0.88rem; color: #fff;">
            ${escapeHtml(url)}
          </a>
        </td>
        <td><span class="badge" style="background: rgba(255,255,255,0.06); color: var(--text-muted);">${sourceBadge}</span></td>
      </tr>
    `;
  }).join("");
}

function handleUrlCheckboxChange(checkbox) {
  const url = checkbox.getAttribute("data-url");
  if (checkbox.checked) {
    selectedUrlsSet.add(url);
  } else {
    selectedUrlsSet.delete(url);
  }
  updateSelectionCounts();
}

function toggleMasterCheckbox(checked) {
  selectAll(checked);
}

function selectAll(shouldSelect) {
  if (shouldSelect) {
    selectedUrlsSet = new Set(discoveredUrlsList);
  } else {
    selectedUrlsSet.clear();
  }
  document.querySelectorAll(".url-chk").forEach(chk => {
    chk.checked = shouldSelect;
  });
  document.getElementById("chkMaster").checked = shouldSelect;
  updateSelectionCounts();
}

function updateSelectionCounts() {
  const count = selectedUrlsSet.size;
  const total = discoveredUrlsList.length;

  document.getElementById("lblSelectedCount").textContent = count;
  document.getElementById("lblSelectedCountSummary").textContent = count;
  document.getElementById("lblTotalCountSummary").textContent = total;

  const btnArchive = document.getElementById("btnStartArchiving");
  btnArchive.disabled = count === 0;
}

function filterUrlsTable() {
  const filter = document.getElementById("urlFilterInput").value.toLowerCase().trim();
  if (!filter) {
    renderDiscoveredTable(discoveredUrlsList);
    return;
  }
  const filtered = discoveredUrlsList.filter(u => u.toLowerCase().includes(filter));
  renderDiscoveredTable(filtered);
}

/**
 * Start the archiving process for selected URLs.
 */
async function startArchiving() {
  if (selectedUrlsSet.size === 0) {
    alert("Please select at least one page to archive.");
    return;
  }

  const websiteUrl = document.getElementById("websiteUrl").value.trim();
  const cfgDelayEl = document.getElementById("cfgArchiveDelay");
  const archiveDelay = cfgDelayEl ? (parseInt(cfgDelayEl.value, 10) || 10) : 10;
  const cfgSkipEl = document.getElementById("cfgSkipDays");
  const skipDays = cfgSkipEl ? (parseInt(cfgSkipEl.value, 10) ?? 30) : 30;

  const urlsToArchive = Array.from(selectedUrlsSet);

  // Setup UI
  document.getElementById("selectionSection").style.display = "none";
  document.getElementById("archivingSection").style.display = "block";
  document.getElementById("completionSection").style.display = "none";

  document.getElementById("progressFraction").textContent = `0 / ${urlsToArchive.length}`;
  document.getElementById("progressBarFill").style.width = "0%";
  document.getElementById("currentUrlDisplay").textContent = "Preparing queue...";
  document.getElementById("currentStageDisplay").textContent = "Initializing Wayback Machine session...";
  document.getElementById("resultsTableBody").innerHTML = "";

  document.getElementById("archivingSection").scrollIntoView({ behavior: "smooth" });

  try {
    const res = await fetch("/api/archive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        website_url: websiteUrl,
        urls: urlsToArchive,
        archive_delay: archiveDelay,
        skip_existing_days: skipDays,
      }),
    });

    if (!res.ok) {
      throw new Error(await res.text());
    }

    const data = await res.json();
    currentJobId = data.job_id;

    if (archivePollInterval) clearInterval(archivePollInterval);
    archivePollInterval = setInterval(pollArchiveStatus, 1500);
  } catch (err) {
    alert("Error starting archiver: " + err.message);
    document.getElementById("selectionSection").style.display = "block";
    document.getElementById("archivingSection").style.display = "none";
  }
}

/**
 * Resume an existing unfinished job.
 */
async function resumeArchivingJob(jobId) {
  try {
    const res = await fetch(`/api/jobs/${jobId}/resume`, { method: "POST" });
    if (!res.ok) {
      alert("Could not resume job: " + (await res.text()));
      return;
    }

    currentJobId = jobId;
    document.getElementById("resumeBanner").style.display = "none";
    document.getElementById("discoverySection").style.display = "none";
    document.getElementById("selectionSection").style.display = "none";
    document.getElementById("archivingSection").style.display = "block";

    if (archivePollInterval) clearInterval(archivePollInterval);
    archivePollInterval = setInterval(pollArchiveStatus, 1500);
  } catch (err) {
    alert("Resume error: " + err.message);
  }
}

/**
 * Poll archive job status and update live progress & table.
 */
async function pollArchiveStatus() {
  if (!currentJobId) return;

  try {
    const res = await fetch(`/api/archive/status/${currentJobId}`);
    if (!res.ok) return;

    const data = await res.json();
    activeJobStatus = data.status;

    const total = data.total || 0;
    const completed = data.completed_count || 0;
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0;

    document.getElementById("progressFraction").textContent = `${completed} / ${total}`;
    document.getElementById("progressBarFill").style.width = `${pct}%`;

    if (data.current_url) {
      document.getElementById("currentUrlDisplay").textContent = data.current_url;
    }
    if (data.current_stage) {
      document.getElementById("currentStageDisplay").textContent = data.current_stage;
    }

    // Update pause button label
    const btnPause = document.getElementById("btnPauseArchive");
    if (data.status === "paused") {
      btnPause.textContent = "Resume";
      btnPause.className = "btn btn-success btn-sm";
    } else {
      btnPause.textContent = "Pause";
      btnPause.className = "btn btn-secondary btn-sm";
    }

    // Update live results table
    renderLiveResults(data.results || []);

    // Check completion
    if (data.status === "completed" || data.status === "failed") {
      clearInterval(archivePollInterval);
      showCompletionCard(data);
    }
  } catch (err) {
    console.error("Poll archive error:", err);
  }
}

function renderLiveResults(results) {
  const tbody = document.getElementById("resultsTableBody");
  tbody.innerHTML = results.map((r, idx) => {
    const status = r.status || "UNKNOWN";
    let badgeCls = "badge-failed";
    if (status === "SUCCESS") badgeCls = "badge-success";
    else if (status === "SKIPPED") badgeCls = "badge-skipped";

    const snapLink = r.archive_url
      ? `<a href="${escapeHtml(r.archive_url)}" target="_blank" rel="noopener" class="link-snap">🌐 View Snapshot &rarr;</a>`
      : `<span style="color: var(--text-muted);">None</span>`;

    return `
      <tr>
        <td style="color: var(--text-muted); font-size: 0.8rem;">${idx + 1}</td>
        <td><a href="${escapeHtml(r.original_url)}" target="_blank" rel="noopener" style="font-family: monospace; font-size: 0.85rem; color: #fff;">${escapeHtml(r.original_url)}</a></td>
        <td><span class="badge ${badgeCls}">${status}</span></td>
        <td>${snapLink}</td>
        <td style="font-size: 0.8rem; color: var(--text-muted); white-space: nowrap;">${escapeHtml(r.capture_timestamp || "—")}</td>
        <td style="font-size: 0.82rem; color: #f87171;">${escapeHtml(r.error || "")}</td>
      </tr>
    `;
  }).join("");
}

function showCompletionCard(jobData) {
  const results = jobData.results || [];
  const total = results.length;
  const success = results.filter(r => r.status === "SUCCESS").length;
  const skipped = results.filter(r => r.status === "SKIPPED").length;
  const failed = results.filter(r => r.status === "FAILED").length;

  document.getElementById("statTotal").textContent = total;
  document.getElementById("statSuccess").textContent = success;
  document.getElementById("statSkipped").textContent = skipped;
  document.getElementById("statFailed").textContent = failed;

  document.getElementById("btnViewReport").href = `/api/jobs/${jobData.id}/report`;
  document.getElementById("btnExportCsv").href = `/api/jobs/${jobData.id}/export/csv`;
  document.getElementById("btnExportJson").href = `/api/jobs/${jobData.id}/export/json`;

  const webUrl = jobData.website_url || "";
  document.getElementById("btnOpenWaybackSite").href = `https://web.archive.org/web/*/${webUrl}`;

  document.getElementById("completionSection").style.display = "block";
  document.getElementById("completionSection").scrollIntoView({ behavior: "smooth" });
}

async function pauseOrResumeArchive() {
  if (!currentJobId) return;
  const action = activeJobStatus === "paused" ? "resume" : "pause";
  try {
    await fetch(`/api/archive/${action}/${currentJobId}`, { method: "POST" });
  } catch (e) {
    console.error("Pause/resume error:", e);
  }
}

async function cancelArchive() {
  if (!confirm("Are you sure you want to stop this archiving run? Progress up to now will be saved.")) return;
  if (!currentJobId) return;
  try {
    await fetch(`/api/archive/cancel/${currentJobId}`, { method: "POST" });
    if (archivePollInterval) clearInterval(archivePollInterval);
    alert("Archiving job stopped.");
  } catch (e) {
    console.error("Cancel error:", e);
  }
}

function resetForNewScan() {
  location.reload();
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
