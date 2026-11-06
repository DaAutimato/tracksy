/*
 * Schmale C-Brücke zu whisper.cpp für den Python-Zugriff per ctypes.
 *
 * whisper_full() erwartet die große Struktur whisper_full_params als Wert;
 * sie in ctypes nachzubauen wäre bei jedem Versionswechsel fehleranfällig.
 * Diese Datei setzt die Parameter in C und bietet nur einfache Typen an.
 *
 * Datenschutz: keine Ausgaben (print_* aus, Log stumm), kein Netzwerk,
 * keine Dateien außer dem Lesen der Modelldatei.
 */
#include <stdbool.h>
#include <string.h>

#include "whisper.h"

#define TW_API __attribute__((visibility("default")))

static void tw_quiet_log(enum ggml_log_level level, const char *text, void *user_data) {
    (void)level;
    (void)text;
    (void)user_data;
}

TW_API int tw_abi_version(void) { return 3; }

TW_API const char *tw_whisper_version(void) { return whisper_version(); }

/* Modell laden; NULL bei Fehler. */
TW_API void *tw_init(const char *model_path) {
    whisper_log_set(tw_quiet_log, NULL);
    struct whisper_context_params cparams = whisper_context_default_params();
    cparams.use_gpu = false;
    cparams.flash_attn = false;
    return whisper_init_from_file_with_params(model_path, cparams);
}

TW_API void tw_free(void *ctx) {
    if (ctx) {
        whisper_free((struct whisper_context *)ctx);
    }
}

/*
 * 16-kHz-Mono-float32 transkribieren. Text (UTF-8, Segmente mit Leerzeichen
 * verbunden) landet in out (immer nullterminiert, ggf. gekürzt).
 * timestamps != 0: Zeitstempel-Token und mehrere Segmente zulassen (Diktat).
 * Ohne Zeitstempel kann der Decoder das Ende der Sprache nicht markieren und
 * wiederholt den letzten Satz bis max_tokens (gemessen: "Server 1 neu
 * gestartet." 37x). Befehle bleiben ohne Zeitstempel (kurz, max_tokens 48).
 * Rückgabe: Länge des Textes in Bytes, < 0 bei Fehler.
 */
TW_API int tw_transcribe(void *ctx, const float *samples, int n_samples,
                         const char *language, const char *initial_prompt,
                         int n_threads, int beam_size, int max_tokens,
                         float temperature, float temperature_inc, int audio_ctx,
                         int timestamps, char *out, int out_len) {
    if (!ctx || !samples || n_samples <= 0 || !out || out_len <= 0) {
        return -1;
    }
    out[0] = '\0';
    struct whisper_context *wctx = (struct whisper_context *)ctx;
    enum whisper_sampling_strategy strategy =
        beam_size > 1 ? WHISPER_SAMPLING_BEAM_SEARCH : WHISPER_SAMPLING_GREEDY;
    struct whisper_full_params p = whisper_full_default_params(strategy);
    if (n_threads > 0) {
        p.n_threads = n_threads;
    }
    p.print_special = false;
    p.print_progress = false;
    p.print_realtime = false;
    p.print_timestamps = false;
    p.translate = false;
    p.no_context = true;
    p.no_timestamps = !timestamps;
    p.single_segment = !timestamps; /* Aufnahmen sind <= 30 s */
    p.language = (language && language[0]) ? language : "de";
    p.detect_language = false;
    p.initial_prompt = (initial_prompt && initial_prompt[0]) ? initial_prompt : NULL;
    p.max_tokens = max_tokens > 0 ? max_tokens : 0;
    /* audio_ctx < 1500 kürzt das 30-s-Fenster des Encoders: kurze Befehle
     * werden mehrfach schneller erkannt (0 = volles Fenster). */
    p.audio_ctx = audio_ctx > 0 && audio_ctx < 1500 ? audio_ctx : 0;
    p.temperature = temperature;
    p.temperature_inc = temperature_inc;
    if (beam_size > 1) {
        p.beam_search.beam_size = beam_size;
    }
    if (whisper_full(wctx, p, samples, n_samples) != 0) {
        return -2;
    }
    int len = 0;
    const int n = whisper_full_n_segments(wctx);
    for (int i = 0; i < n; ++i) {
        const char *seg = whisper_full_get_segment_text(wctx, i);
        if (!seg) {
            continue;
        }
        while (*seg == ' ') {
            ++seg;
        }
        int seg_len = (int)strlen(seg);
        if (seg_len == 0) {
            continue;
        }
        if (len > 0 && len < out_len - 1) {
            out[len++] = ' ';
        }
        int room = out_len - 1 - len;
        if (room <= 0) {
            break;
        }
        if (seg_len > room) {
            seg_len = room;
        }
        memcpy(out + len, seg, (size_t)seg_len);
        len += seg_len;
    }
    out[len] = '\0';
    return len;
}
