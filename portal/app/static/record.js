(function () {
  "use strict";
  var section = document.getElementById("record-section");
  if (!section) return;

  // Capability gate. Recording only activates when the browser's
  // MediaRecorder can both capture camera/mic AND encode directly to a
  // container our backend's duration validator can read. media.py
  // deliberately parses only ISO-BMFF (MP4/MOV "mvhd" atom), not WebM/EBML
  // — extending that is a separate, larger change we didn't make here to
  // avoid risking the stable file-upload path. Most Chrome/Firefox builds
  // only offer WebM via MediaRecorder, so on those browsers this section
  // stays hidden and file upload remains the only path — see
  // docs/portal-notes.md "Browser recording" for the full rationale.
  var MIME = "video/mp4";
  var secureContext = !!(window.isSecureContext && navigator.mediaDevices);
  var supported = !!(
    window.MediaRecorder &&
    secureContext &&
    navigator.mediaDevices.getUserMedia &&
    MediaRecorder.isTypeSupported &&
    MediaRecorder.isTypeSupported(MIME)
  );
  if (!supported) {
    // Explain why, rather than just vanishing with no trace -- this is
    // itself an honest status, not a broken feature: file upload remains
    // fully available either way. Distinguish the two real causes rather
    // than one generic message, since the fix differs (HTTPS vs browser).
    section.style.display = "block";
    var reason;
    if (!secureContext) {
      reason = "Camera/mic recording needs a secure (HTTPS) connection, which this deployment " +
        "doesn't have yet -- use file upload below instead.";
    } else if (!window.MediaRecorder || !navigator.mediaDevices.getUserMedia) {
      reason = "This browser doesn't support camera/mic recording here -- use file upload below instead.";
    } else {
      reason = "This browser can't record directly to a format this server accepts yet -- use file upload below instead.";
    }
    section.innerHTML = "<h3>Record a new video</h3>";
    var p = document.createElement("p");
    p.className = "muted small";
    p.textContent = reason;
    section.appendChild(p);
    return;
  }

  section.style.display = "block";

  var liveVideo = document.getElementById("record-live");
  var playback = document.getElementById("record-playback");
  var timerEl = document.getElementById("record-timer");
  var statusEl = document.getElementById("record-status");
  var btnStart = document.getElementById("btn-start-record");
  var btnStop = document.getElementById("btn-stop-record");
  var btnDiscard = document.getElementById("btn-discard-record");
  var btnSubmit = document.getElementById("btn-submit-record");

  var stream = null;
  var recorder = null;
  var chunks = [];
  var blob = null;
  var objectUrl = null;
  var timerInterval = null;
  var startedAt = 0;

  function fmtTime(sec) {
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec % 60);
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  function stopStreamTracks() {
    if (stream) {
      var tracks = stream.getTracks();
      for (var i = 0; i < tracks.length; i++) tracks[i].stop();
      stream = null;
    }
  }

  function resetUI() {
    btnStart.style.display = "";
    btnStart.disabled = false;
    btnStop.style.display = "none";
    btnDiscard.style.display = "none";
    btnSubmit.style.display = "none";
    btnSubmit.disabled = false;
    timerEl.style.display = "none";
    liveVideo.style.display = "none";
    playback.style.display = "none";
    playback.removeAttribute("src");
  }
  resetUI();

  btnStart.addEventListener("click", function () {
    // The permission prompt happens here — only after this explicit click,
    // never on page load.
    btnStart.disabled = true;
    statusEl.textContent = "Requesting camera/microphone access...";
    navigator.mediaDevices.getUserMedia({ video: true, audio: true }).then(
      function (s) {
        stream = s;
        liveVideo.srcObject = stream;
        liveVideo.style.display = "block";
        chunks = [];
        recorder = new MediaRecorder(stream, { mimeType: MIME });
        recorder.ondataavailable = function (e) {
          if (e.data && e.data.size) chunks.push(e.data);
        };
        recorder.onstop = function () {
          blob = new Blob(chunks, { type: MIME });
          objectUrl = URL.createObjectURL(blob);
          playback.src = objectUrl;
          playback.style.display = "block";
          liveVideo.style.display = "none";
          btnDiscard.style.display = "";
          btnSubmit.style.display = "";
          statusEl.textContent = "Recording ready — preview above, then upload or discard.";
        };
        recorder.start();
        startedAt = Date.now();
        timerEl.style.display = "";
        timerEl.textContent = "00:00";
        timerInterval = setInterval(function () {
          timerEl.textContent = fmtTime((Date.now() - startedAt) / 1000);
        }, 250);
        btnStart.style.display = "none";
        btnStop.style.display = "";
        statusEl.textContent = "Recording...";
      },
      function (err) {
        btnStart.disabled = false;
        statusEl.textContent =
          "Camera/microphone access was not granted (" + err.name + "). Use file upload instead.";
      }
    );
  });

  btnStop.addEventListener("click", function () {
    if (recorder && recorder.state !== "inactive") recorder.stop();
    clearInterval(timerInterval);
    stopStreamTracks();
    btnStop.style.display = "none";
  });

  function discard() {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    blob = null;
    chunks = [];
    clearInterval(timerInterval);
    stopStreamTracks();
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.stop();
      } catch (e) {
        /* already stopped */
      }
    }
    resetUI();
  }
  btnDiscard.addEventListener("click", discard);

  btnSubmit.addEventListener("click", function () {
    if (!blob) return;
    btnSubmit.disabled = true;
    btnDiscard.disabled = true;
    statusEl.textContent = "Uploading...";
    var fd = new FormData();
    fd.append("file", blob, "recording.mp4");
    var modeSel = document.querySelector('select[name="word_replacement_mode"]');
    var modelSel = document.querySelector('select[name="voice_model"]');
    var presetSel = document.querySelector('select[name="voice_preset"]');
    if (modeSel) fd.append("word_replacement_mode", modeSel.value);
    if (modelSel) fd.append("voice_model", modelSel.value);
    if (presetSel) fd.append("voice_preset", presetSel.value);
    fetch("/upload", { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (resp) {
        if (resp.ok && resp.redirected) {
          window.location.href = resp.url;
          return;
        }
        if (resp.ok) {
          window.location.reload();
          return;
        }
        statusEl.textContent =
          "Upload rejected (status " + resp.status + ") — the recording may be too " +
          "short/long, or its length could not be read. Try again or use file upload.";
        btnSubmit.disabled = false;
        btnDiscard.disabled = false;
      })
      .catch(function () {
        statusEl.textContent = "Upload failed — check your connection and try again.";
        btnSubmit.disabled = false;
        btnDiscard.disabled = false;
      });
  });

  // Never leave the camera/mic open if the user navigates away mid-recording.
  window.addEventListener("beforeunload", stopStreamTracks);
})();
