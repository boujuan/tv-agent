package com.boujuan.tvagent;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.Arrays;

/**
 * Opened by the remote's mic button. The TV opens the remote's microphone on that press and hands it to an app
 * recording for voice recognition (Phase 0). The audio streams to tv-agent on the laptop while you speak; it shows
 * what it has understood, decides during your pause and says when to stop. Then the pill shows what it will do and
 * closes. Nothing keeps running afterwards. Without the stream (an older agent, a broken connection) it records until
 * you stop talking and sends it all at once, as before.
 */
public class ListenActivity extends Activity {
    private static final int RATE = 16000;
    private static final int FRAME = RATE / 50;            // 20 ms
    private static final int PREROLL_FRAMES = 15;          // keep 300 ms before speech starts
    private static final int START_FRAMES = 3;             // 60 ms above threshold = speech
    private static final int END_FRAMES = 35;              // 700 ms of quiet after speech = done (no stream)
    private static final int STREAM_END_FRAMES = 75;       // streaming: the laptop decides; 1.5 s is a safety net
    private static final int GIVE_UP_FRAMES = 200;         // 4 s without speech
    private static final int MAX_FRAMES = 500;             // 10 s cap

    private static final int TRACK_DP = 260;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private TextView status, detail;
    private View level;
    private volatile boolean running, sendNow;
    private double shownLevel = 0.02;
    // Where tv-agent runs, the token it expects and its stream port, set by `tv-agent tv pair` (Pairing).
    private static volatile String agentUrl = "", agentToken = "";
    private static volatile int streamPort;
    private int levelFrames;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        buildUi();
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            done("Microphone permission missing", "", 3000);
            return;
        }
        agentUrl = Pairing.url(this);
        agentToken = Pairing.token(this);
        streamPort = Pairing.streamPort(this);
        if (agentUrl.isEmpty() || agentToken.isEmpty()) {
            done("Not paired with tv-agent", "On the computer: tv-agent tv pair", 3500);
            return;
        }
        running = true;
        if (streamPort <= 0) {
            new Thread(() -> post("/v1/listen/start", new byte[0], 1000), "listen-start").start();
        }
        new Thread(this::listen, "listen").start();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        sendNow = true;     // mic pressed again: stop listening and send what we have
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            running = false;
            finish();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onStop() {
        super.onStop();
        running = false;
        finish();
    }

    private int dp(float v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private void buildUi() {
        FrameLayout root = new FrameLayout(this);
        LinearLayout pill = new LinearLayout(this);
        pill.setOrientation(LinearLayout.VERTICAL);
        pill.setGravity(Gravity.CENTER_HORIZONTAL);
        pill.setPadding(dp(28), dp(14), dp(28), dp(14));
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0xE6141820);
        bg.setCornerRadius(dp(28));
        bg.setStroke(dp(1), 0x33FFFFFF);
        pill.setBackground(bg);

        status = new TextView(this);
        status.setTextColor(Color.WHITE);
        status.setTextSize(TypedValue.COMPLEX_UNIT_SP, 22);
        status.setText("Listening…");
        detail = new TextView(this);
        detail.setTextColor(0xB3FFFFFF);
        detail.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        detail.setVisibility(View.GONE);
        status.setGravity(Gravity.CENTER);
        detail.setGravity(Gravity.CENTER);
        // The level bar fills a fixed track, so the pill itself never changes size while you talk.
        FrameLayout track = new FrameLayout(this);
        GradientDrawable trackBg = new GradientDrawable();
        trackBg.setColor(0x26FFFFFF);
        trackBg.setCornerRadius(dp(2));
        track.setBackground(trackBg);
        level = new View(this);
        GradientDrawable bar = new GradientDrawable();
        bar.setColor(0xFF6EA8FF);
        bar.setCornerRadius(dp(2));
        level.setBackground(bar);
        level.setPivotX(dp(TRACK_DP) / 2f);
        level.setScaleX(0.02f);
        track.addView(level, new FrameLayout.LayoutParams(dp(TRACK_DP), dp(4)));

        pill.addView(status);
        pill.addView(detail);
        LinearLayout.LayoutParams tl = new LinearLayout.LayoutParams(dp(TRACK_DP), dp(4));
        tl.topMargin = dp(10);
        pill.addView(track, tl);
        // Over a list (ChoicesActivity), the pill goes bottom-right so the numbers stay in view.
        boolean besideList = ChoicesActivity.showing;
        pill.setMinimumWidth(dp(besideList ? 360 : 420));
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM | (besideList ? Gravity.END : Gravity.CENTER_HORIZONTAL));
        lp.bottomMargin = dp(48);
        lp.rightMargin = besideList ? dp(48) : 0;
        root.addView(pill, lp);
        setContentView(root);
    }

    /** Fast rise, slow fall, drawn every third 20 ms frame: steady enough to read, no layout passes. */
    private void showLevel(double db) {
        double target = Math.max(0.02, Math.min(1.0, (db + 60) / 45));
        shownLevel = target > shownLevel ? target : shownLevel * 0.8 + target * 0.2;
        if (++levelFrames % 3 != 0) return;
        float scale = (float) shownLevel;
        ui.post(() -> level.setScaleX(scale));
    }

    /** What the laptop has understood so far, under "Listening…". */
    private void showHeard(String text) {
        ui.post(() -> {
            if (!running || text.isEmpty()) return;
            detail.setText("“" + text + "”");
            detail.setVisibility(View.VISIBLE);
        });
    }

    private void done(String main, String sub, long closeAfterMs) {
        ui.post(() -> {
            status.setText(main);
            detail.setText(sub);
            detail.setVisibility(sub.isEmpty() ? View.GONE : View.VISIBLE);
            ((View) level.getParent()).setVisibility(View.GONE);
            ui.postDelayed(this::finish, closeAfterMs);
        });
    }

    private static double db(short[] pcm, int n) {
        double sum = 0;
        for (int i = 0; i < n; i++) sum += (double) pcm[i] * pcm[i];
        double rms = Math.sqrt(sum / Math.max(1, n)) / 32768.0;
        return rms > 0 ? 20 * Math.log10(rms) : -120;
    }

    private void listen() {
        // No audio focus: asking for it as an assistant made players pause when the pill opened and play again when
        // it closed, which undid "pause". tv-agent lowers the TV's volume itself while you speak.
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec;
        try {
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, Math.max(min, RATE));
        } catch (RuntimeException e) {
            done("Microphone unavailable", "", 2500);
            return;
        }
        if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
            rec.release();
            done("Microphone unavailable", "", 2500);
            return;
        }
        AudioStream stream = openStream();
        // Kept even while streaming: if the connection breaks, this is sent the old way.
        ByteArrayOutputStream speech = new ByteArrayOutputStream(RATE * 2 * 6);
        ArrayDeque<short[]> preroll = new ArrayDeque<>();
        double[] early = new double[10];
        int earlyCount = 0, frames = 0, loud = 0, quiet = 0;
        double threshold = -45;
        boolean speaking = false;
        short[] frame = new short[FRAME];
        rec.startRecording();
        try {
            while (running && frames < MAX_FRAMES && !sendNow && (stream == null || !stream.stop)) {
                int got = rec.read(frame, 0, FRAME);
                if (got <= 0) break;
                frames++;
                if (stream != null && (stream.stop || !stream.send(frame, got))) {
                    // The laptop decided and closed the connection: a failed write then means done, not broken
                    // (sending the recording again the old way ran every command twice). Its reply can trail.
                    if (stream.stop || stream.awaitReply(500) != null) break;
                    stream.close();
                    stream = null;      // really broken: record until you stop talking and send it all
                }
                double level = db(frame, got);
                showLevel(level);
                // Background level from the first 200 ms that carry sound (the remote's mic opens a moment late).
                if (earlyCount < early.length && level > -110) {
                    early[earlyCount++] = level;
                    if (earlyCount == early.length) {
                        double[] s = early.clone();
                        Arrays.sort(s);
                        threshold = Math.max(-55, Math.min(-30, s[2] + 12));
                    }
                }
                if (!speaking) {
                    preroll.addLast(Arrays.copyOf(frame, got));
                    if (preroll.size() > PREROLL_FRAMES) preroll.removeFirst();
                    loud = level > threshold ? loud + 1 : 0;
                    if (loud >= START_FRAMES) {
                        speaking = true;
                        for (short[] f : preroll) speech.write(toBytes(f, f.length), 0, f.length * 2);
                        preroll.clear();
                    } else if (frames >= GIVE_UP_FRAMES) {
                        break;
                    }
                } else {
                    speech.write(toBytes(frame, got), 0, got * 2);
                    quiet = level < threshold - 3 ? quiet + 1 : 0;
                    if (quiet >= (stream != null ? STREAM_END_FRAMES : END_FRAMES)) break;
                }
            }
        } finally {
            rec.stop();
            rec.release();
        }
        if (!running) {
            if (stream != null) stream.close();
            return;
        }
        ui.post(() -> {
            status.setText("…");
            ((View) level.getParent()).setVisibility(View.GONE);
        });
        String reply = null;
        if (stream != null) {
            stream.end();
            reply = stream.awaitReply(8000);
            stream.close();
        }
        if (reply == null) {
            if (!speaking) {
                done("Didn't hear anything", "", 1500);
                return;
            }
            reply = post("/v1/utterance", speech.toByteArray(), 8000);
        }
        if (reply == null) {
            done("Agent offline", "Is tv-agent running on the laptop?", 2500);
            return;
        }
        try {
            JSONObject r = new JSONObject(reply);
            if (r.has("error")) {
                boolean busy = "busy".equals(r.optString("error"));
                done(busy ? "Still busy with the last one" : "Not paired with tv-agent",
                        busy ? "" : "On the computer: tv-agent tv pair", 2500);
                return;
            }
            String heard = r.optString("transcript", "");
            done(r.optString("say", "OK"), heard.isEmpty() ? "" : "“" + heard + "”",
                    r.optBoolean("ok", false) ? 1400 : 2600);
        } catch (Exception e) {
            done("Agent error", "", 2500);
        }
    }

    private static byte[] toBytes(short[] pcm, int n) {
        byte[] b = new byte[n * 2];
        for (int i = 0; i < n; i++) {
            b[2 * i] = (byte) pcm[i];
            b[2 * i + 1] = (byte) (pcm[i] >> 8);
        }
        return b;
    }

    /** The live connection, or null when this pairing has no stream port or the laptop doesn't answer at once. */
    private AudioStream openStream() {
        if (streamPort <= 0) return null;
        try {
            Socket s = new Socket();
            s.connect(new InetSocketAddress(Uri.parse(agentUrl).getHost(), streamPort), 800);
            s.setTcpNoDelay(true);
            s.setSoTimeout(12000);
            AudioStream stream = new AudioStream(s);
            byte[] hello = (new JSONObject().put("token", agentToken).put("rate", RATE) + "\n")
                    .getBytes(StandardCharsets.UTF_8);
            stream.out.write(hello);
            stream.out.flush();
            new Thread(stream::readReplies, "listen-replies").start();
            return stream;
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * Audio to tv-agent while you speak (protocol in the server's server.py): frames of a 2-byte length and PCM16,
     * a 0 length to end; back come JSON lines: what it heard so far, "stop" once it has decided, then the reply.
     */
    private final class AudioStream {
        private final Socket socket;
        private final DataOutputStream out;
        volatile boolean stop;          // the laptop has decided: stop recording
        private volatile boolean broken;
        private String reply;           // guarded by this

        AudioStream(Socket socket) throws IOException {
            this.socket = socket;
            this.out = new DataOutputStream(new BufferedOutputStream(socket.getOutputStream(), 4096));
        }

        boolean send(short[] pcm, int n) {
            if (broken) return false;
            try {
                out.writeShort(n * 2);
                out.write(toBytes(pcm, n));
                out.flush();
                return true;
            } catch (IOException e) {
                broken = true;
                return false;
            }
        }

        void end() {
            try {
                out.writeShort(0);
                out.flush();
            } catch (IOException ignored) {
                // the reply (if any) still arrives, or awaitReply gives up
            }
        }

        void readReplies() {
            try (BufferedReader in = new BufferedReader(
                    new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = in.readLine()) != null; ) {
                    JSONObject m = new JSONObject(line);
                    if (m.has("heard")) showHeard(m.optString("heard", ""));
                    if (m.optBoolean("stop", false)) stop = true;
                    if (m.has("say") || m.has("error")) {
                        synchronized (this) {
                            reply = line;
                            notifyAll();
                        }
                        stop = true;
                        return;
                    }
                }
            } catch (Exception ignored) {
                // a broken connection: recording goes on and is sent the old way
            }
            broken = true;
            synchronized (this) {
                notifyAll();
            }
        }

        String awaitReply(long ms) {
            long until = System.currentTimeMillis() + ms;
            synchronized (this) {
                while (reply == null && !broken && System.currentTimeMillis() < until) {
                    try {
                        wait(Math.max(1, until - System.currentTimeMillis()));
                    } catch (InterruptedException e) {
                        break;
                    }
                }
                return reply;
            }
        }

        void close() {
            try {
                socket.close();
            } catch (IOException ignored) {
                // closing anyway
            }
        }
    }

    /** POST to tv-agent; the reply body, or null if the laptop didn't answer. */
    private static String post(String path, byte[] body, int readTimeoutMs) {
        return post(agentUrl, agentToken, path, body, readTimeoutMs);
    }

    private static String post(String base, String token, String path, byte[] body, int readTimeoutMs) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + path).openConnection();
            c.setConnectTimeout(1500);
            c.setReadTimeout(readTimeoutMs);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("Authorization", "Bearer " + token);
            c.setRequestProperty("Content-Type", "audio/L16; rate=16000; channels=1");
            c.setFixedLengthStreamingMode(body.length);
            try (OutputStream o = c.getOutputStream()) {
                o.write(body);
            }
            if (c.getResponseCode() != 200) return null;
            try (InputStream in = c.getInputStream()) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[4096];
                for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
                return out.toString(StandardCharsets.UTF_8);
            }
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }
}
