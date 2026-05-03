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
    });
    socket.on("scan_completed", (payload = {}) => {
      if (sawCompletion) return;
      sawCompletion = true;
      setScanLoading(false);
      showToast("Library scan complete. Refreshing...");
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
  if (!player) return;

  const videoId = player.dataset.videoId;
  const playlistId = player.dataset.playlistId || null;
  const resumePosition = Number(player.dataset.resumePosition || 0);
  if (resumePosition > 0) {
    player.currentTime = resumePosition;
  }
  const tryAutoplay = () => {
    const maybePromise = player.play();
    if (maybePromise && typeof maybePromise.catch === "function") {
      maybePromise.catch(() => {});
    }
  };
  if (player.readyState >= 2) tryAutoplay();
  else player.addEventListener("loadeddata", tryAutoplay, { once: true });

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

  window.addEventListener("beforeunload", () => {
    if (heartbeat) clearInterval(heartbeat);
    if (shouldTrackProgress && !player.paused) {
      sendProgress().catch(() => {});
    }
  });

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
    select.innerHTML = "<option value=''>Select a playlist...</option>";
    (result.playlists || []).forEach((playlist) => {
      const option = document.createElement("option");
      option.value = playlist.playlist_id;
      option.textContent = playlist.name;
      select.appendChild(option);
    });
  };

  const closeAllMenus = () => {
    document.querySelectorAll(".row-menu").forEach((menu) => menu.classList.add("hidden"));
  };

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".row-actions")) closeAllMenus();
  });

  document.querySelectorAll(".row-menu-trigger").forEach((trigger) => {
    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const rowActions = trigger.closest(".row-actions");
      const menu = rowActions?.querySelector(".row-menu");
      if (!menu) return;
      const opening = menu.classList.contains("hidden");
      closeAllMenus();
      if (opening) menu.classList.remove("hidden");
    });
  });

  document.querySelectorAll(".row-menu-item").forEach((button) => {
    button.addEventListener("click", async (event) => {
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
        if (action === "add-to-playlist" && dialog && submitButton) {
          await populatePlaylists();
          if (newPlaylistInput) newPlaylistInput.value = "";
          submitButton.onclick = async (e) => {
            e.preventDefault();
            const payload = {};
            const selected = select?.value;
            const created = (newPlaylistInput?.value || "").trim();
            if (created) payload.new_playlist_name = created;
            else if (selected) payload.playlist_id = selected;
            else return;
            await requestJSON(`/api/video/${encodeURIComponent(videoId)}/add-to-playlist`, {
              method: "POST",
              body: JSON.stringify(payload),
            });
            dialog.close();
            showToast("Saved to playlist");
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
    });
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
          window.location.href = "/playlists";
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

setupScanButton();
setupUserMenu();
setupThemeToggle();
setupVideoPlayer();
setupVideoRowActions();
setupLocalDates();
