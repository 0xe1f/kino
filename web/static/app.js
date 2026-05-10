// Copyright 2026 Akop Karapetyan
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

async function requestJSON(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (_err) {
    data = {};
  }
  if (!response.ok) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

function isAuthenticatedUser() {
  return document.body?.dataset?.isAuthenticated === "true";
}

function showToast(message) {
  const root = document.getElementById("toast-root");
  if (!root || !message) return;
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.textContent = message;
  root.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 2200);
}

function setupUserMenu() {
  const menu = document.querySelector(".user-menu");
  if (!menu) return;
  document.addEventListener("click", (event) => {
    if (!menu.contains(event.target)) {
      menu.open = false;
    }
  });
}

function setupDialogBackdropClose(dialog) {
  if (!dialog) return;
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

function setScanLoading(isLoading) {
  const loading = document.getElementById("scan-loading");
  const button = document.getElementById("scan-trigger");
  if (loading) loading.classList.toggle("hidden", !isLoading);
  if (button) button.disabled = isLoading;
}

function setupScanButton() {
  const button = document.getElementById("scan-trigger");
  if (!button) return;

  let sawCompletion = false;

  button.addEventListener("click", async () => {
    setScanLoading(true);
    sawCompletion = false;
    try {
      await requestJSON("/api/scan/trigger", { method: "POST" });
    } catch (err) {
      showToast(`Scan error: ${err.message || err}`);
      setScanLoading(false);
    }
  });

  if (typeof io === "function") {
    const socket = io();
    socket.on("scan_started", () => {
      setScanLoading(true);
      const loadingEl = document.getElementById("scan-loading");
      if (loadingEl) loadingEl.textContent = "Scanning...";
    });
    socket.on("scan_progress", (payload = {}) => {
      const loadingEl = document.getElementById("scan-loading");
      if (!loadingEl) return;
      if (typeof payload.processed === "number" && typeof payload.total === "number" && payload.total > 0) {
        const pct = Math.round((payload.processed / payload.total) * 100);
        loadingEl.textContent = `Scanning... ${pct}%`;
      }
    });
    socket.on("scan_completed", (payload = {}) => {
      if (sawCompletion) return;
      sawCompletion = true;
      setScanLoading(false);
      showToast("Video Archive scan complete. Refreshing...");
      setTimeout(() => window.location.reload(), 800);
    });
    socket.on("scan_failed", (payload = {}) => {
      setScanLoading(false);
      showToast(`Scan failed: ${payload.error || "unknown error"}`);
    });
  }

  requestJSON("/api/scan/status")
    .then((status) => {
      if (status.running) {
        setScanLoading(true);
        const loadingEl = document.getElementById("scan-loading");
        if (loadingEl && status.progress) {
          const p = status.progress;
          if (typeof p.processed === "number" && typeof p.total === "number" && p.total > 0) {
            const pct = Math.round((p.processed / p.total) * 100);
            loadingEl.textContent = `Scanning... ${pct}%`;
          }
        }
      }
    })
    .catch(() => {});
}

function setupThemeToggle() {
  const toggle = document.getElementById("theme-toggle");
  const icon = document.getElementById("theme-toggle-icon");
  if (!toggle) return;

  const applyTheme = (theme) => {
    document.documentElement.setAttribute("data-theme", theme);
    if (icon) {
      icon.textContent = theme === "day" ? "wb_sunny" : "dark_mode";
    }
  };

  toggle.addEventListener("click", async () => {
    const currentTheme = document.documentElement.getAttribute("data-theme") || "night";
    const nextTheme = currentTheme === "day" ? "night" : "day";
    try {
      const result = await requestJSON("/api/theme", {
        method: "POST",
        body: JSON.stringify({ theme: nextTheme }),
      });
      applyTheme(result.theme || nextTheme);
    } catch (err) {
      alert(String(err.message || err));
    }
  });
}

function setupVideoPlayer() {
  const player = document.getElementById("video-player");
  if (!player) return null;

  const videoId = player.dataset.videoId;
  const playlistId = player.dataset.playlistId || null;
  const resumePosition = Number(player.dataset.resumePosition || 0);

  // Seek to resume position as soon as metadata is available (readyState >= 1).
  // This must happen before canplay so the seek is already applied when we play.
  if (resumePosition > 0) {
    if (player.readyState >= 1) {
      player.currentTime = resumePosition;
    } else {
      player.addEventListener("loadedmetadata", () => {
        player.currentTime = resumePosition;
      }, { once: true });
    }
  }

  // Play once the browser has buffered enough data to begin (canplay / readyState >= 3).
  // For AJAX switches, switchVideo already called play() within the click handler's
  // activation window; this fires again when data arrives and is a no-op if already playing.
  const onCanPlay = () => { player.play().catch(() => {}); };
  if (player.readyState >= 3) {
    onCanPlay();
  } else {
    player.addEventListener("canplay", onCanPlay, { once: true });
  }

  requestJSON(`/api/video/${encodeURIComponent(videoId)}/play`, {
    method: "POST",
    body: JSON.stringify({ playlist_id: playlistId }),
  })
    .then((result) => {
      const view = document.getElementById("view-count");
      if (view) view.textContent = String(result.views ?? view.textContent);
    })
    .catch(() => {});

  const shouldTrackProgress = isAuthenticatedUser();
  const sendProgress = async () => {
    await requestJSON(`/api/video/${encodeURIComponent(videoId)}/progress`, {
      method: "POST",
      body: JSON.stringify({
        playlist_id: playlistId,
        position_seconds: player.currentTime,
      }),
    });
  };

  let heartbeat = null;
  if (shouldTrackProgress) {
    heartbeat = setInterval(() => {
      if (!player.paused) {
        sendProgress().catch(() => {});
      }
    }, 10000);
  }

  const onBeforeUnload = () => {
    if (heartbeat) clearInterval(heartbeat);
    if (shouldTrackProgress && !player.paused) {
      sendProgress().catch(() => {});
    }
  };
  window.addEventListener("beforeunload", onBeforeUnload);

  const onEnded = () => {
    const nextLink = document.querySelector("a[data-nav-next]");
    if (!nextLink) return;
    const url = new URL(nextLink.href, window.location.href);
    const pathMatch = url.pathname.match(/^\/video\/(.+)$/);
    if (!pathMatch) return;
    const nextVideoId = decodeURIComponent(pathMatch[1]);
    const nextPlaylistId = url.searchParams.get("playlist_id") || null;
    switchVideo(nextVideoId, nextPlaylistId);
  };
  player.addEventListener("ended", onEnded);

  const likeButton = document.getElementById("like-toggle");
  if (likeButton) {
    likeButton.addEventListener("click", async () => {
      try {
        const result = await requestJSON(`/api/video/${encodeURIComponent(videoId)}/reaction`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        const like = document.getElementById("like-count");
        if (like) like.textContent = String(result.likes ?? like.textContent);
        if (result.liked) {
          likeButton.classList.add("liked");
        } else {
          likeButton.classList.remove("liked");
        }
      } catch (err) {
        alert(String(err.message || err));
      }
    });
  }

  return function teardown() {
    if (heartbeat) clearInterval(heartbeat);
    window.removeEventListener("beforeunload", onBeforeUnload);
    player.removeEventListener("canplay", onCanPlay);
    player.removeEventListener("ended", onEnded);
    if (shouldTrackProgress && !player.paused) {
      sendProgress().catch(() => {});
    }
  };
}

let activeTeardown = null;

async function switchVideo(videoId, playlistId) {
  if (activeTeardown) {
    activeTeardown();
    activeTeardown = null;
  }

  const params = new URLSearchParams({ playlist_id: playlistId || "" });
  let data;
  try {
    data = await requestJSON(`/api/video/${encodeURIComponent(videoId)}/watch-data?${params}`);
  } catch (err) {
    showToast(`Failed to load video: ${err.message || err}`);
    return;
  }

  const player = document.getElementById("video-player");
  if (!player) return;

  player.dataset.videoId = data.video_id;
  player.dataset.playlistId = playlistId || "";
  player.dataset.resumePosition = data.resume_position || 0;
  player.poster = data.poster || "";
  player.src = data.src;
  // Kick off play() synchronously here so it runs before any further async
  // work; this keeps us within the browser's user-activation window.
  player.play().catch(() => {});

  const titleEl = document.querySelector(".video-title");
  if (titleEl) titleEl.textContent = data.title || "";

  const viewEl = document.getElementById("view-count");
  if (viewEl) viewEl.textContent = String(data.views ?? 0);

  const likeCountEl = document.getElementById("like-count");
  if (likeCountEl) likeCountEl.textContent = String(data.likes ?? 0);

  const likeToggle = document.getElementById("like-toggle");
  if (likeToggle) likeToggle.classList.toggle("liked", !!data.is_liked);

  const addedTimeEl = document.querySelector(".added-time time.local-date");
  if (addedTimeEl && data.created_at) {
    addedTimeEl.setAttribute("datetime", data.created_at);
    const date = new Date(data.created_at);
    if (!isNaN(date)) {
      addedTimeEl.textContent = date.toLocaleDateString(undefined, {
        year: "numeric", month: "long", day: "numeric",
      });
    }
  }

  const descEl = document.getElementById("video-description");
  if (descEl) descEl.textContent = data.description || "";

  _updateNavControls(data.playlist_nav, playlistId);
  _updateSidebarActiveRow(videoId);

  const newUrl = playlistId
    ? `/video/${encodeURIComponent(videoId)}?playlist_id=${encodeURIComponent(playlistId)}`
    : `/video/${encodeURIComponent(videoId)}`;
  history.pushState({ videoId, playlistId: playlistId || null }, "", newUrl);

  activeTeardown = setupVideoPlayer();
}

function _updateNavControls(playlistNav, playlistId) {
  const prevEl = document.querySelector("[data-nav-prev]");
  const nextEl = document.querySelector("[data-nav-next]");

  function replaceNavSlot(el, videoId) {
    if (!el) return;
    if (videoId) {
      const url = playlistId
        ? `/video/${encodeURIComponent(videoId)}?playlist_id=${encodeURIComponent(playlistId)}`
        : `/video/${encodeURIComponent(videoId)}`;
      if (el.tagName === "A") {
        el.href = url;
      } else {
        const a = document.createElement("a");
        a.className = "menu-item";
        a.textContent = el.textContent;
        a.href = url;
        if (el.hasAttribute("data-nav-prev")) a.setAttribute("data-nav-prev", "");
        if (el.hasAttribute("data-nav-next")) a.setAttribute("data-nav-next", "");
        el.replaceWith(a);
      }
    } else {
      if (el.tagName !== "SPAN") {
        const span = document.createElement("span");
        span.className = "muted";
        span.textContent = el.textContent;
        if (el.hasAttribute("data-nav-prev")) span.setAttribute("data-nav-prev", "");
        if (el.hasAttribute("data-nav-next")) span.setAttribute("data-nav-next", "");
        el.replaceWith(span);
      }
    }
  }

  if (playlistNav) {
    replaceNavSlot(prevEl, playlistNav.previous_video_id);
    replaceNavSlot(nextEl, playlistNav.next_video_id);
  }
}

function _updateSidebarActiveRow(videoId) {
  const list = document.querySelector("ul[data-nav-playlist-id]");
  if (!list) return;
  list.dataset.navCurrentVideoId = videoId;
  list.querySelectorAll(".video-row").forEach((row) => {
    const isActive = row.dataset.videoId === videoId;
    row.classList.toggle("active-row", isActive);
    const prefix = row.querySelector(".row-prefix");
    if (prefix) {
      prefix.textContent = isActive ? "▶" : (prefix.dataset.positionLabel ?? prefix.textContent);
    }
  });
  list.querySelector(".active-row")?.scrollIntoView({ block: "nearest" });
}

function setupVideoNavInterception() {
  const list = document.querySelector("ul[data-nav-playlist-id]");
  if (!list) return;

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (!link) return;

    const inNavList = list.contains(link) && link.classList.contains("row-link");
    const isNavControl = link.hasAttribute("data-nav-prev") || link.hasAttribute("data-nav-next");
    if (!inNavList && !isNavControl) return;

    const playlistId = list.dataset.navPlaylistId || null;
    let videoId = null;

    if (inNavList) {
      const row = link.closest(".video-row");
      videoId = row?.dataset?.videoId || null;
    } else {
      const url = new URL(link.href, window.location.href);
      const pathMatch = url.pathname.match(/^\/video\/(.+)$/);
      if (pathMatch) videoId = decodeURIComponent(pathMatch[1]);
    }

    if (!videoId) return;
    event.preventDefault();
    switchVideo(videoId, playlistId);
  });

  window.addEventListener("popstate", (event) => {
    const state = event.state;
    if (state && state.videoId) {
      switchVideo(state.videoId, state.playlistId || null);
    } else {
      window.location.reload();
    }
  });
}

let lastPlaylist = null;

function updateQuickAddButtons() {
  document.querySelectorAll("[data-action='quick-add-to-playlist']").forEach((btn) => {
    if (lastPlaylist) {
      btn.textContent = `Add to ${lastPlaylist.name}`;
      btn.dataset.playlistId = lastPlaylist.id;
      btn.classList.remove("hidden");
    } else {
      btn.classList.add("hidden");
    }
  });
}

async function fetchLastPlaylist() {
  try {
    const result = await requestJSON("/api/user/playlists");
    const lastId = result.last_playlist_id;
    const match = lastId
      ? (result.playlists || []).find((p) => p.playlist_id === lastId)
      : null;
    lastPlaylist = match ? { id: match.playlist_id, name: match.name } : null;
    updateQuickAddButtons();
  } catch (_err) {}
}

function setupVideoRowActions() {
  if (!isAuthenticatedUser()) return;
  const dialog = document.getElementById("playlist-dialog");
  const select = document.getElementById("playlist-select");
  const newPlaylistInput = document.getElementById("new-playlist-name");
  const submitButton = document.getElementById("playlist-dialog-submit");
  const removeDialog = document.getElementById("remove-dialog");
  const removeSubmitButton = document.getElementById("remove-dialog-submit");
  let pendingRemove = null;
  setupDialogBackdropClose(dialog);
  setupDialogBackdropClose(removeDialog);

  const populatePlaylists = async () => {
    if (!select) return;
    const result = await requestJSON("/api/user/playlists");
    const lastId = result.last_playlist_id;
    select.innerHTML = "<option value=''>Select a playlist...</option>";
    (result.playlists || []).forEach((playlist) => {
      const option = document.createElement("option");
      option.value = playlist.playlist_id;
      option.textContent = playlist.name;
      select.appendChild(option);
    });
    if (lastId) {
      select.value = lastId;
    }
    const match = lastId
      ? (result.playlists || []).find((p) => p.playlist_id === lastId)
      : null;
    lastPlaylist = match ? { id: match.playlist_id, name: match.name } : null;
    updateQuickAddButtons();
  };

  const nameFeedback = document.getElementById("new-playlist-name-feedback");
  let nameCheckTimer = null;

  const setNameFeedback = (message, isError) => {
    if (!nameFeedback) return;
    nameFeedback.textContent = message;
    nameFeedback.style.color = isError ? "var(--color-danger, red)" : "";
    if (submitButton) submitButton.disabled = isError;
  };

  if (newPlaylistInput) {
    newPlaylistInput.addEventListener("input", () => {
      clearTimeout(nameCheckTimer);
      const val = newPlaylistInput.value.trim();
      if (!val) {
        setNameFeedback("", false);
        return;
      }
      nameCheckTimer = setTimeout(async () => {
        try {
          const result = await requestJSON(
            `/api/user/playlists/name-check?name=${encodeURIComponent(val)}`
          );
          if (newPlaylistInput.value.trim() !== val) return;
          if (result.available) {
            setNameFeedback("", false);
          } else {
            setNameFeedback(result.reason || "Name already taken", true);
          }
        } catch (_err) {
          setNameFeedback("", false);
        }
      }, 400);
    });
  }

  const closeAllMenus = () => {
    document.querySelectorAll(".row-menu").forEach((menu) => menu.classList.add("hidden"));
  };

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".row-actions")) closeAllMenus();
  });

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest(".row-menu-trigger");
    if (!trigger) return;
    event.preventDefault();
    event.stopPropagation();
    const menu = trigger.closest(".row-actions")?.querySelector(".row-menu");
    if (!menu) return;
    const opening = menu.classList.contains("hidden");
    closeAllMenus();
    if (opening) menu.classList.remove("hidden");
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".row-menu-item");
    if (!button) return;
    {
      event.preventDefault();
      event.stopPropagation();
      const action = button.dataset.action;
      const row = button.closest(".video-row");
      const videoId = row?.dataset?.videoId;
      const contextPlaylistId = row?.dataset?.contextPlaylistId;
      if (!videoId) return;
      closeAllMenus();
      try {
        if (action === "save-watch-later") {
          const result = await requestJSON(`/api/video/${encodeURIComponent(videoId)}/builtin`, {
            method: "POST",
            body: JSON.stringify({ kind: "watch_later" }),
          });
          showToast(result.added ? "Added to Watch Later" : "Removed from Watch Later");
          return;
        }
        if (action === "save-favorite") {
          const result = await requestJSON(`/api/video/${encodeURIComponent(videoId)}/builtin`, {
            method: "POST",
            body: JSON.stringify({ kind: "favorites" }),
          });
          showToast(result.added ? "Added to Favorites" : "Removed from Favorites");
          return;
        }
        if (action === "quick-add-to-playlist") {
          const playlistId = button.dataset.playlistId;
          if (!playlistId) return;
          try {
            await requestJSON(`/api/video/${encodeURIComponent(videoId)}/add-to-playlist`, {
              method: "POST",
              body: JSON.stringify({ playlist_id: playlistId }),
            });
            showToast("Saved to playlist");
          } catch (err) {
            showToast(err.message || "Failed to add to playlist");
          }
          return;
        }
        if (action === "add-to-playlist" && dialog && submitButton) {
          await populatePlaylists();
          if (newPlaylistInput) newPlaylistInput.value = "";
          setNameFeedback("", false);
          submitButton.onclick = async (e) => {
            e.preventDefault();
            const payload = {};
            const selected = select?.value;
            const created = (newPlaylistInput?.value || "").trim();
            if (created) payload.new_playlist_name = created;
            else if (selected) payload.playlist_id = selected;
            else return;
            try {
              await requestJSON(`/api/video/${encodeURIComponent(videoId)}/add-to-playlist`, {
                method: "POST",
                body: JSON.stringify(payload),
              });
              dialog.close();
              showToast("Saved to playlist");
            } catch (err) {
              dialog.close();
              showToast(err.message || "Failed to add to playlist");
            }
          };
          dialog.showModal();
          return;
        }
        if (action === "remove-from-playlist" && removeDialog && removeSubmitButton) {
          if (!contextPlaylistId) return;
          pendingRemove = { row, videoId, contextPlaylistId };
          removeDialog.showModal();
          return;
        }
      } catch (err) {
        alert(String(err.message || err));
      }
    }
  });

  if (removeSubmitButton && removeDialog) {
    removeSubmitButton.addEventListener("click", async (event) => {
      event.preventDefault();
      if (!pendingRemove) {
        removeDialog.close();
        return;
      }
      try {
        await requestJSON(
          `/api/playlist/${encodeURIComponent(pendingRemove.contextPlaylistId)}/remove-video`,
          {
            method: "POST",
            body: JSON.stringify({ video_id: pendingRemove.videoId }),
          }
        );

        const row = pendingRemove.row;
        const currentVideoPlayer = document.getElementById("video-player");
        const currentVideoId = currentVideoPlayer?.dataset?.videoId || null;
        const isCurrentVideo = currentVideoId === pendingRemove.videoId;

        const list = row?.closest(".list");
        const rowLink = row?.querySelector(".row-link");
        const remainingLinks = Array.from(list?.querySelectorAll(".video-row .row-link") || []);
        const currentIndex = rowLink ? remainingLinks.indexOf(rowLink) : -1;
        const fallbackLink =
          currentIndex >= 0 && currentIndex < remainingLinks.length - 1
            ? remainingLinks[currentIndex + 1]
            : remainingLinks[0];

        row?.remove();
        const stillHasRows = !!list?.querySelector(".video-row");
        removeDialog.close();
        showToast("Removed from playlist");

        if (!stillHasRows) {
          window.location.href = "/history";
          return;
        }
        if (isCurrentVideo && fallbackLink?.href) {
          window.location.href = fallbackLink.href;
          return;
        }
      } catch (err) {
        alert(String(err.message || err));
      } finally {
        pendingRemove = null;
      }
    });
  }
}

function setupLocalDates() {
  document.querySelectorAll("time.local-date[datetime]").forEach((el) => {
    const date = new Date(el.getAttribute("datetime"));
    if (isNaN(date)) return;
    el.textContent = date.toLocaleDateString(undefined, {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
  });
}

function setupLoadMore() {
  async function triggerLoadMore(btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    const url = btn.dataset.url;
    const bookmark = btn.dataset.bookmark || "";
    const start = parseInt(btn.dataset.start || "0", 10);
    const originalText = btn.textContent;
    btn.textContent = "Loading...";
    try {
      const params = new URLSearchParams({ bookmark, start });
      const data = await requestJSON(`${url}?${params}`);
      const list = btn.closest("section").querySelector("ul.list");
      list.insertAdjacentHTML("beforeend", data.html);
      if (data.has_more) {
        btn.dataset.bookmark = data.next_bookmark;
        btn.dataset.start = data.next_start;
        btn.disabled = false;
        btn.textContent = "Load more";
      } else {
        btn.remove();
      }
    } catch (err) {
      showToast(`Failed to load more: ${err.message}`);
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) triggerLoadMore(entry.target);
    });
  }, { rootMargin: "0px 0px 200px 0px" });

  document.querySelectorAll("[data-load-more]").forEach((btn) => observer.observe(btn));

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-load-more]");
    if (btn) triggerLoadMore(btn);
  });
}

function setupNavItems() {
  const list = document.querySelector("ul[data-nav-playlist-id]");
  if (!list) return;
  const pid = list.dataset.navPlaylistId;
  const vid = list.dataset.navCurrentVideoId;
  fetch(`/api/playlist/${encodeURIComponent(pid)}/nav-items?current_video_id=${encodeURIComponent(vid)}`)
    .then((r) => r.json())
    .then((data) => {
      list.innerHTML = data.html;
      list.querySelector(".active-row")?.scrollIntoView({ block: "nearest" });
    })
    .catch(() => {
      list.innerHTML = '<li class="nav-items-loading"><span class="muted">Failed to load.</span></li>';
    });
}

setupScanButton();
setupUserMenu();
setupThemeToggle();
activeTeardown = setupVideoPlayer();
setupVideoRowActions();
setupLocalDates();
setupLoadMore();
setupNavItems();
setupVideoNavInterception();
if (isAuthenticatedUser()) fetchLastPlaylist();
