(function () {
  "use strict";
  var root = document.getElementById("job-root");
  if (!root) return;
  var jobId = root.dataset.jobId;

  // Mirrors the real backend state machine (config.JOB_STATES) plus the
  // "edit" pause layered on top of "transcribing" via awaiting_user_edit —
  // see jobs.py's module docstring. Every step here is a real state the
  // worker actually reports; there is no invented in-between progress.
  var STEP_ORDER = ["queued", "transcribing", "edit", "generating", "blending", "exporting", "done"];
  var stepperEl = document.getElementById("stepper");

  function stepKey(job) {
    if (job.state === "transcribing") return job.awaiting_user_edit ? "edit" : "transcribing";
    if (job.state === "error") {
      var seen = [];
      var history = job.state_history || [];
      for (var i = 0; i < history.length; i++) seen.push(history[i].state);
      for (var j = STEP_ORDER.length - 1; j >= 0; j--) {
        if (seen.indexOf(STEP_ORDER[j]) !== -1) return STEP_ORDER[j];
      }
      return "queued";
    }
    if (STEP_ORDER.indexOf(job.state) !== -1) return job.state;
    return "queued";
  }

  function renderStepper(job) {
    if (!stepperEl) return;
    var idx = STEP_ORDER.indexOf(stepKey(job));
    var items = stepperEl.getElementsByTagName("li");
    for (var i = 0; i < items.length; i++) {
      var li = items[i];
      var liIdx = STEP_ORDER.indexOf(li.getAttribute("data-step"));
      li.className = li.className.replace(/\s*(active|complete)\b/g, "");
      if (liIdx < idx) li.className += " complete";
      else if (liIdx === idx) li.className += " active";
    }
    stepperEl.className = "stepper" + (job.state === "error" ? " stepper-error" : "");
  }

  var stateBadge = document.getElementById("state-badge");
  var stageElapsed = document.getElementById("stage-elapsed");
  var stageEstimate = document.getElementById("stage-estimate");
  var errorBox = document.getElementById("error-box");
  var transcriptHint = document.getElementById("transcript-hint");
  var transcriptText = document.getElementById("transcript-text");
  var btnFruit = document.getElementById("btn-fruit");
  var btnSubmit = document.getElementById("btn-submit-edit");
  var replacementNote = document.getElementById("replacement-note");
  var videoAfter = document.getElementById("video-after");
  var afterHint = document.getElementById("after-hint");
  var downloadLink = document.getElementById("download-link");
  var purgeBtn = document.getElementById("btn-purge");
  var purgeHint = document.getElementById("purge-hint");
  var selectWordMode = document.getElementById("select-word-mode");
  var selectVoiceModel = document.getElementById("select-voice-model");
  var selectVoicePreset = document.getElementById("select-voice-preset");
  var licenseNote = document.getElementById("license-note");
  var engineResultLine = document.getElementById("engine-result-line");

  var VOICE_MODELS = {};
  var LICENSE_NOTES = {};
  try {
    var vmEl = document.getElementById("voice-models-data");
    if (vmEl) VOICE_MODELS = JSON.parse(vmEl.textContent);
    var lnEl = document.getElementById("license-notes-data");
    if (lnEl) LICENSE_NOTES = JSON.parse(lnEl.textContent);
  } catch (e) { /* malformed registry data would be a server bug, not fatal here */ }

  var transcriptLoaded = false;
  var editSubmitted = false;
  var lastReplacements = [];
  var afterLoaded = false;
  var optionsInitialized = false;

  function setEditingEnabled(enabled) {
    transcriptText.disabled = !enabled;
    btnFruit.disabled = !enabled;
    btnSubmit.disabled = !enabled;
    if (selectWordMode) selectWordMode.disabled = !enabled;
    if (selectVoiceModel) selectVoiceModel.disabled = !enabled;
    if (selectVoicePreset) selectVoicePreset.disabled = !enabled;
  }

  // Rebuilds the preset <select> for whichever engine is currently chosen
  // in selectVoiceModel, and shows/hides the license note for that engine
  // (e.g. F5-TTS's noncommercial-checkpoint restriction) — never silently
  // omitted. Called on load (seeded from the job's current values) and
  // whenever the user changes the engine dropdown.
  function refreshPresetOptions(selectedPreset) {
    var modelId = selectVoiceModel.value;
    var model = VOICE_MODELS[modelId];
    selectVoicePreset.innerHTML = "";
    if (model) {
      Object.keys(model.presets).forEach(function (presetId) {
        var opt = document.createElement("option");
        opt.value = presetId;
        opt.textContent = model.presets[presetId].label;
        if (presetId === (selectedPreset || model.default_preset)) opt.selected = true;
        selectVoicePreset.appendChild(opt);
      });
    }
    if (LICENSE_NOTES[modelId]) {
      licenseNote.textContent = LICENSE_NOTES[modelId];
      licenseNote.style.display = "";
    } else {
      licenseNote.style.display = "none";
    }
  }
  if (selectVoiceModel) {
    selectVoiceModel.addEventListener("change", function () { refreshPresetOptions(); });
  }

  function render(job) {
    stateBadge.textContent = job.state + (job.awaiting_user_edit ? " (edit needed)" : "");
    stateBadge.className = "badge state-" + job.state;
    renderStepper(job);
    if (purgeBtn) {
      purgeBtn.disabled = !!job.claimed_active;
      purgeHint.style.display = job.claimed_active ? "" : "none";
    }
    // Once a job is terminal (done/error), the backend freezes elapsed
    // against finished_at instead of wall-clock now (see jobs.py's
    // to_public_dict), so this stops advancing on reload/poll. We also
    // switch to showing the job's whole-run elapsed time rather than the
    // now-meaningless "current stage" figure, and drop the estimate line
    // entirely rather than show a stale/confusing "estimating...".
    if (job.is_terminal) {
      stageElapsed.textContent = "completed in " + job.elapsed_total_sec + "s";
      stageEstimate.textContent = "";
    } else {
      stageElapsed.textContent = "elapsed: " + job.elapsed_current_stage_sec + "s";
      stageEstimate.textContent = "estimate: " + job.estimate_label;
    }

    if (job.error_message) {
      errorBox.style.display = "block";
      errorBox.textContent = "Error: " + job.error_message +
        (job.retryable ? " (will retry automatically)" : " (not retryable)");
    } else {
      errorBox.style.display = "none";
    }

    if (job.transcript && !transcriptLoaded && !editSubmitted) {
      transcriptText.value = job.transcript.text;
      transcriptLoaded = true;
      transcriptHint.textContent = job.transcript.mock
        ? "Mock transcript (model pipeline not yet wired in) — edit freely below."
        : "Transcript ready — edit freely below.";
    }

    // Seed the run-options selects from the job's current values exactly
    // once — after that, this is the user's own in-progress choice, never
    // overwritten by a later poll.
    if (!optionsInitialized && selectVoiceModel) {
      if (selectWordMode) selectWordMode.value = job.word_replacement_mode;
      selectVoiceModel.value = job.voice_model;
      refreshPresetOptions(job.voice_preset);
      optionsInitialized = true;
    }

    if (job.engine_result && job.engine_result.engine) {
      engineResultLine.textContent = "Actually ran: " + job.engine_result.engine +
        (job.engine_result.engine_revision ? " (checkpoint " + job.engine_result.engine_revision.slice(0, 12) + ")" : "") +
        (job.engine_result.device ? " on " + job.engine_result.device : "");
      engineResultLine.style.display = "";
    }

    if (job.state === "transcribing" && job.awaiting_user_edit && !editSubmitted) {
      setEditingEnabled(true);
    } else if (!editSubmitted) {
      setEditingEnabled(false);
      if (job.state === "queued" || (job.state === "transcribing" && !job.awaiting_user_edit)) {
        transcriptHint.textContent = "Waiting for the worker to transcribe this job...";
      }
    }

    if (job.state !== "transcribing" || !job.awaiting_user_edit) {
      editSubmitted = editSubmitted || job.state === "generating" || job.state === "blending" ||
        job.state === "exporting" || job.state === "done";
    }

    if (job.has_output && !afterLoaded) {
      videoAfter.src = "/jobs/" + jobId + "/media/result";
      afterHint.textContent = "Your edited video — play it to compare against the original above.";
      downloadLink.href = "/jobs/" + jobId + "/download";
      downloadLink.classList.remove("disabled");
      afterLoaded = true;
    }
  }

  var pollTimer = null;

  function poll() {
    fetch("/api/jobs/" + jobId, { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 401) {
          window.location.href = "/login";
          return null;
        }
        if (r.status === 403) {
          window.location.href = "/change-password";
          return null;
        }
        return r.json();
      })
      .then(function (job) {
        if (!job) return;
        render(job);
        // Stop polling once terminal — nothing further will change, and
        // this is what makes elapsed truly stop ticking on this page
        // (not just display a frozen number while still polling).
        if (job.is_terminal && pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      })
      .catch(function () { /* transient network hiccup; next poll retries */ });
  }

  btnFruit.addEventListener("click", function () {
    fetch("/api/jobs/" + jobId + "/fruit-replace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ text: transcriptText.value }),
    })
      .then(function (r) { return r.json(); })
      .then(function (result) {
        transcriptText.value = result.text;
        lastReplacements = result.replacements;
        if (!result.replacements.length) {
          replacementNote.textContent = "No suitable words found to replace — edit the text manually.";
        } else {
          replacementNote.textContent = "Replaced: " + result.replacements
            .map(function (r2) { return r2.original + " -> " + r2.replacement; })
            .join(", ");
        }
      });
  });

  btnSubmit.addEventListener("click", function () {
    setEditingEnabled(false);
    var payload = { text: transcriptText.value, replacements: lastReplacements };
    if (selectWordMode) payload.word_replacement_mode = selectWordMode.value;
    if (selectVoiceModel) payload.voice_model = selectVoiceModel.value;
    if (selectVoicePreset) payload.voice_preset = selectVoicePreset.value;
    fetch("/api/jobs/" + jobId + "/submit-edit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || "submit failed"); });
        return r.json();
      })
      .then(function (job) {
        editSubmitted = true;
        render(job);
      })
      .catch(function (err) {
        setEditingEnabled(true);
        replacementNote.textContent = "Could not submit: " + err.message;
      });
  });

  poll();
  pollTimer = setInterval(poll, 2000);
})();
