package com.boujuan.tvagent;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.MicrophoneInfo;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.widget.TextView;

import java.io.DataOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * Phase 0 probe. Records one 5 s take from whichever microphone Android gives a normal app for voice
 * recognition, then writes the WAV and one JSON line of facts (routed device, active microphones, levels)
 * to /sdcard/Android/data/com.boujuan.tvagent/files/probe/ for the laptop to pull.
 */
public class ProbeActivity extends Activity {
    private static final int RATE = 16000;
    private static final int SECONDS = 5;
    private static final int WINDOW = RATE / 10;   // 100 ms level windows

    private final Handler ui = new Handler(Looper.getMainLooper());
    private TextView text;
    private volatile boolean running;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        text = new TextView(this);
        text.setTextSize(34);
        text.setTextColor(Color.WHITE);
        text.setBackgroundColor(0xE0101018);
        text.setGravity(Gravity.CENTER);
        setContentView(text);
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            show("No microphone permission for TV agent.");
            ui.postDelayed(this::finish, 5000);
            return;
        }
        running = true;
        new Thread(this::record, "probe-record").start();
    }

    @Override
    protected void onStop() {
        super.onStop();
        running = false;   // Home or Back ends the take early; what was recorded is still saved
    }

    private void show(String s) {
        ui.post(() -> text.setText(s));
    }

    private void record() {
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec;
        try {
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, Math.max(min, RATE * 2));
        } catch (RuntimeException e) {
            show("Microphone unavailable:\n" + e.getMessage());
            ui.postDelayed(this::finish, 5000);
            return;
        }
        if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
            rec.release();
            show("Microphone unavailable (not initialised).");
            ui.postDelayed(this::finish, 5000);
            return;
        }
        short[] pcm = new short[RATE * SECONDS];
        int n = 0;
        rec.startRecording();
        while (running && n < pcm.length) {
            int got = rec.read(pcm, n, Math.min(WINDOW, pcm.length - n));
            if (got <= 0) break;
            n += got;
            show(String.format(Locale.ROOT, "Speak now  (%d s left)\nlevel %.0f dB",
                    (pcm.length - n) / RATE + 1, dbfs(pcm, n - got, n)));
        }
        AudioDeviceInfo routed = rec.getRoutedDevice();
        List<MicrophoneInfo> active = null;
        try {
            active = rec.getActiveMicrophones();
        } catch (IOException ignored) {
        }
        rec.stop();
        rec.release();

        String name = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT).format(new Date());
        File dir = getExternalFilesDir("probe");
        try {
            writeWav(new File(dir, "take-" + name + ".wav"), pcm, n);
            try (FileWriter log = new FileWriter(new File(dir, "log.jsonl"), true)) {
                log.write(facts(name, routed, active, pcm, n) + "\n");
            }
            show(String.format(Locale.ROOT, "Saved take %s\nspeech %.0f dB, background %.0f dB",
                    name, percentileDb(pcm, n, 0.95), percentileDb(pcm, n, 0.10)));
        } catch (IOException e) {
            show("Could not save: " + e.getMessage());
        }
        ui.postDelayed(this::finish, 2500);
    }

    /** RMS level of pcm[from, to) in dB below full scale; -120 for digital silence. */
    private static double dbfs(short[] pcm, int from, int to) {
        double sum = 0;
        for (int i = from; i < to; i++) sum += (double) pcm[i] * pcm[i];
        double rms = Math.sqrt(sum / Math.max(1, to - from)) / 32768.0;
        return rms > 0 ? 20 * Math.log10(rms) : -120;
    }

    /** The level that the given fraction of 100 ms windows stay under: 0.10 ~ background, 0.95 ~ speech. */
    private static double percentileDb(short[] pcm, int n, double fraction) {
        int windows = n / WINDOW;
        if (windows == 0) return -120;
        double[] levels = new double[windows];
        for (int w = 0; w < windows; w++) levels[w] = dbfs(pcm, w * WINDOW, (w + 1) * WINDOW);
        Arrays.sort(levels);
        return levels[Math.min(windows - 1, (int) (fraction * windows))];
    }

    private String facts(String name, AudioDeviceInfo routed, List<MicrophoneInfo> active, short[] pcm, int n) {
        StringBuilder s = new StringBuilder("{\"take\":\"").append(name).append('"');
        s.append(",\"samples\":").append(n);
        s.append(String.format(Locale.ROOT, ",\"speech_db\":%.1f,\"background_db\":%.1f,\"peak_db\":%.1f",
                percentileDb(pcm, n, 0.95), percentileDb(pcm, n, 0.10), percentileDb(pcm, n, 1.0)));
        s.append(",\"routed\":").append(routed == null ? "null" : device(routed));
        s.append(",\"active_mics\":[");
        if (active != null) {
            for (int i = 0; i < active.size(); i++) {
                MicrophoneInfo m = active.get(i);
                if (i > 0) s.append(',');
                s.append("{\"description\":").append(q(m.getDescription()))
                        .append(",\"address\":").append(q(m.getAddress()))
                        .append(",\"type\":").append(m.getType())
                        .append(",\"location\":").append(m.getLocation()).append('}');
            }
        }
        s.append("],\"inputs\":[");
        AudioDeviceInfo[] inputs = getSystemService(AudioManager.class).getDevices(AudioManager.GET_DEVICES_INPUTS);
        for (int i = 0; i < inputs.length; i++) {
            if (i > 0) s.append(',');
            s.append(device(inputs[i]));
        }
        return s.append("]}").toString();
    }

    private static String device(AudioDeviceInfo d) {
        return "{\"id\":" + d.getId() + ",\"type\":" + d.getType() + ",\"product\":" + q(String.valueOf(d.getProductName()))
                + ",\"address\":" + q(d.getAddress()) + "}";
    }

    private static String q(String v) {
        if (v == null) return "null";
        StringBuilder s = new StringBuilder("\"");
        for (char c : v.toCharArray()) {
            if (c == '"' || c == '\\') s.append('\\').append(c);
            else if (c < 0x20) s.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
            else s.append(c);
        }
        return s.append('"').toString();
    }

    private static void writeWav(File file, short[] pcm, int n) throws IOException {
        try (DataOutputStream out = new DataOutputStream(new FileOutputStream(file))) {
            int bytes = n * 2;
            out.writeBytes("RIFF");
            out.writeInt(Integer.reverseBytes(36 + bytes));
            out.writeBytes("WAVEfmt ");
            out.writeInt(Integer.reverseBytes(16));
            out.writeShort(Short.reverseBytes((short) 1));          // PCM
            out.writeShort(Short.reverseBytes((short) 1));          // mono
            out.writeInt(Integer.reverseBytes(RATE));
            out.writeInt(Integer.reverseBytes(RATE * 2));           // byte rate
            out.writeShort(Short.reverseBytes((short) 2));          // block align
            out.writeShort(Short.reverseBytes((short) 16));         // bits per sample
            out.writeBytes("data");
            out.writeInt(Integer.reverseBytes(bytes));
            for (int i = 0; i < n; i++) out.writeShort(Short.reverseBytes(pcm[i]));
        }
    }
}
