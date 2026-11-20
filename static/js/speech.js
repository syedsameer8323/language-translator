(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);

    const form = $("translate-form");
    const textarea = $("source-text");
    const sourceSelect = $("source-language");
    const targetSelect = $("target-language");
    const swapButton = $("swap-languages");
    const micButton = $("mic-button");
    const micLabel = $("mic-label");
    const clearButton = $("clear-button");
    const copyButton = $("copy-button");
    const listenButton = $("listen-button");
    const translateButton = $("translate-button");
    const autoTranslate = $("auto-translate");
    const counter = $("char-count");
    const speechStatus = $("speech-status");
    const resultArea = $("result-area");
    const detectedNote = $("detected-note");
    const errorBox = $("error-box");
    const errorText = $("error-text");
    const stateBadge = $("state-badge");
    const stateText = $("state-text");

    if (!form || !textarea) return;

    const MAX_CHARS = textarea.maxLength > 0 ? textarea.maxLength : 5000;
    const FALLBACK_SPEECH_LANG = "en-IN";
    const RTL_LANGS = new Set(["ur"]);

    let lastTranslation = "";
    let textChangedSinceTranslate = false;

    /* ------------------------------------------------------------------
       Small UI helpers
    ------------------------------------------------------------------ */
    function setState(state, label) {
        stateBadge.dataset.state = state;
        stateText.textContent = label;
    }

    function setSpeechStatus(message, tone = "") {
        speechStatus.textContent = message;
        speechStatus.className = "speech-status" + (tone ? " " + tone : "");
    }

    function showError(message) {
        errorText.textContent = message;
        errorBox.hidden = false;
    }

    function clearError() {
        errorBox.hidden = true;
        errorText.textContent = "";
    }

    function updateCount() {
        counter.textContent = `${textarea.value.length} / ${MAX_CHARS}`;
    }

    function selectedOption(select) {
        return select.options[select.selectedIndex];
    }

    function sourceSpeechLang() {
        const option = selectedOption(sourceSelect);
        return option.dataset.speech || "";
    }

    /* ------------------------------------------------------------------
       Translation
    ------------------------------------------------------------------ */
    function showResult(text, detected, targetCode) {
        lastTranslation = text;
        resultArea.innerHTML = "";
        const out = document.createElement("div");
        out.className = "translated-output";
        out.id = "translated-output";
        out.textContent = text; // textContent: never interpreted as HTML
        out.dir = RTL_LANGS.has(targetCode) ? "rtl" : "ltr";
        resultArea.appendChild(out);

        detectedNote.textContent = "";
        if (detected && detected.length) {
            detectedNote.append("Detected: ");
            const strong = document.createElement("strong");
            strong.textContent = detected.join(" + ");
            detectedNote.append(strong);
        }
        copyButton.disabled = false;
        listenButton.disabled = false;
    }

    function resetResult() {
        lastTranslation = "";
        resultArea.innerHTML =
            '<div class="empty-result"><span class="empty-icon">文</span><p>Your translation will appear here.</p></div>';
        detectedNote.textContent = "";
        copyButton.disabled = true;
        listenButton.disabled = true;
    }

    async function translate() {
        const text = textarea.value.trim();
        clearError();

        if (!text) {
            showError("Type, paste or speak something to translate.");
            return;
        }

        translateButton.disabled = true;
        translateButton.textContent = "Translating…";
        setState("working", "Translating…");

        try {
            const response = await fetch(form.dataset.api || "/api/translate/", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": form.querySelector("[name=csrfmiddlewaretoken]").value,
                },
                body: JSON.stringify({
                    text,
                    source: sourceSelect.value,
                    target: targetSelect.value,
                }),
            });
            const data = await response.json().catch(() => ({}));

            if (!response.ok) {
                showError(data.error || "Translation couldn't be completed. Please try again.");
                setState("error", "Needs attention");
                return;
            }

            showResult(data.translated, data.detected, targetSelect.value);
            textChangedSinceTranslate = false;
            setState("ready", "Done");
        } catch (err) {
            showError("Couldn't reach the server. Check your internet connection and try again.");
            setState("error", "Needs attention");
        } finally {
            translateButton.disabled = false;
            translateButton.textContent = "Translate";
        }
    }

    // Intercept the form so the page doesn't reload (the plain POST still works without JS).
    form.dataset.api = "/api/translate/";
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        translate();
    });

    textarea.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
            event.preventDefault();
            translate();
        }
    });

    textarea.addEventListener("input", () => {
        updateCount();
        textChangedSinceTranslate = true;
    });

    /* ------------------------------------------------------------------
       Language controls
    ------------------------------------------------------------------ */
    function updateMicHint() {
        if (listening) return;
        if (!sourceSpeechLang()) {
            setSpeechStatus(
                "Auto-detect is on. The microphone will listen in English. Pick a language in “From” to speak in another one."
            );
        } else {
            setSpeechStatus(`The microphone will listen in ${selectedOption(sourceSelect).text.split(" · ")[0]}.`);
        }
    }

    sourceSelect.addEventListener("change", updateMicHint);

    swapButton.addEventListener("click", () => {
        const from = sourceSelect.value;
        const to = targetSelect.value;
        const newTarget = from === "auto" ? (to === "en" ? "hi" : "en") : from;

        sourceSelect.value = to;
        targetSelect.value = newTarget;

        // Move the translation into the input box, like most translators do.
        if (lastTranslation) {
            textarea.value = lastTranslation;
            updateCount();
            resetResult();
            textChangedSinceTranslate = true;
        }
        updateMicHint();
    });

    /* ------------------------------------------------------------------
       Copy / Listen / Clear
    ------------------------------------------------------------------ */
    copyButton.addEventListener("click", async () => {
        if (!lastTranslation) return;
        try {
            await navigator.clipboard.writeText(lastTranslation);
        } catch (err) {
            const temp = document.createElement("textarea");
            temp.value = lastTranslation;
            document.body.appendChild(temp);
            temp.select();
            document.execCommand("copy");
            temp.remove();
        }
        const original = copyButton.textContent;
        copyButton.textContent = "Copied";
        setTimeout(() => (copyButton.textContent = original), 1400);
    });

    listenButton.addEventListener("click", () => {
        if (!lastTranslation || !("speechSynthesis" in window)) {
            showError("Your browser can't read text aloud.");
            return;
        }
        if (window.speechSynthesis.speaking) {
            window.speechSynthesis.cancel();
            return;
        }
        clearError();
        const utterance = new SpeechSynthesisUtterance(lastTranslation);
        utterance.lang = selectedOption(targetSelect).dataset.speech || "en-IN";
        window.speechSynthesis.speak(utterance);
    });

    clearButton.addEventListener("click", () => {
        stopListening(false);
        textarea.value = "";
        updateCount();
        resetResult();
        clearError();
        setState("ready", "Ready");
        textarea.focus();
    });

    /* ------------------------------------------------------------------
       Speech recognition
    ------------------------------------------------------------------ */
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    let recognition = null;
    let listening = false;       // the user wants the mic on
    let fatalError = false;
    let baseText = "";
    let finalText = "";

    function setMicUi(on) {
        micButton.classList.toggle("active", on);
        micButton.setAttribute("aria-pressed", String(on));
        micLabel.textContent = on ? "Stop" : "Speak";
        sourceSelect.disabled = on;
        swapButton.disabled = on;
    }

    if (!SpeechRecognition) {
        micButton.disabled = true;
        setSpeechStatus("Voice input isn't supported in this browser. Use Chrome or Edge, or type your text.", "error");
    } else {
        recognition = new SpeechRecognition();
        recognition.continuous = true;
        recognition.interimResults = true;
        recognition.maxAlternatives = 1;

        recognition.onstart = () => {
            setMicUi(true);
            setState("listening", "Listening…");
            setSpeechStatus("Listening. Speak clearly, then tap Stop.", "listening");
        };

        recognition.onresult = (event) => {
            let interim = "";
            for (let i = event.resultIndex; i < event.results.length; i += 1) {
                const piece = event.results[i][0].transcript;
                if (event.results[i].isFinal) {
                    finalText = `${finalText} ${piece.trim()}`.trim();
                } else {
                    interim += piece;
                }
            }
            textarea.value = [baseText, finalText, interim.trim()].filter(Boolean).join(" ").trim().slice(0, MAX_CHARS);
            updateCount();
            textChangedSinceTranslate = true;
        };

        recognition.onerror = (event) => {
            const messages = {
                "not-allowed": "Microphone access was blocked. Allow the microphone in your browser's address bar, then try again.",
                "service-not-allowed": "Microphone access was blocked. Allow the microphone in your browser settings, then try again.",
                "audio-capture": "No microphone was found. Connect one and try again.",
                "network": "Voice input needs an internet connection. Check your connection and try again.",
                "language-not-supported": "This browser can't listen in the chosen language. Pick another “From” language.",
            };
            if (event.error === "no-speech") {
                setSpeechStatus("Haven't heard anything yet. Try speaking a little louder.", "listening");
                return; // keep listening
            }
            if (event.error === "aborted") return;

            fatalError = true;
            setSpeechStatus(messages[event.error] || `Voice input stopped (${event.error}).`, "error");
        };

        recognition.onend = () => {
            // Chrome ends the session by itself after a pause. Quietly restart unless the user stopped it.
            if (listening && !fatalError) {
                baseText = textarea.value.trim();
                finalText = "";
                try {
                    recognition.start();
                    return;
                } catch (err) {
                    /* fall through to a normal stop */
                }
            }
            finishListening();
        };
    }

    function finishListening() {
        listening = false;
        setMicUi(false);
        if (fatalError) {
            setState("error", "Needs attention");
            return;
        }
        setState("ready", "Ready");
        updateMicHint();
        if (autoTranslate.checked && textarea.value.trim() && textChangedSinceTranslate) {
            translate();
        }
    }

    function startListening() {
        if (!recognition) return;
        clearError();
        fatalError = false;
        baseText = textarea.value.trim();
        finalText = "";
        recognition.lang = sourceSpeechLang() || FALLBACK_SPEECH_LANG;
        listening = true;
        try {
            recognition.start();
        } catch (err) {
            listening = false;
            setSpeechStatus("Couldn't start the microphone. Please try again.", "error");
        }
    }

    function stopListening(translateAfter = true) {
        if (!recognition || !listening) return;
        if (!translateAfter) textChangedSinceTranslate = false;
        listening = false;
        recognition.stop(); // onend -> finishListening()
        setSpeechStatus("Finishing up…");
    }

    micButton.addEventListener("click", () => {
        if (listening) stopListening(true);
        else startListening();
    });

    window.addEventListener("beforeunload", () => {
        if (recognition && listening) recognition.abort();
        if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    });

    /* ------------------------------------------------------------------
       Initial state
    ------------------------------------------------------------------ */
    updateCount();
    if (copyButton && !lastTranslation) {
        const existing = $("translated-output");
        if (existing) lastTranslation = existing.textContent;
    }
    if (SpeechRecognition) updateMicHint();
})();
