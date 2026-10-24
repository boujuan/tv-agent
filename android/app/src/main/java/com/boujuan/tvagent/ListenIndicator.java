package com.boujuan.tvagent;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.os.SystemClock;
import android.util.TypedValue;
import android.view.View;

/**
 * The pill's animation. Listening: bars that move with your voice. Listening over: the bars gather into the middle,
 * then three dots while the agent works. Then a tick (done), a cross (not done), or the bars fall away (cancelled).
 * It floats over 4K video, so it allocates nothing per frame and redraws only while something moves, at 30 fps at
 * most: with the pill up the TV composes the pill and the player's own layer on its GPU, again for every frame drawn
 * (the video keeps its plane and loses no frames).
 */
final class ListenIndicator extends View {
    private static final int LISTENING = 0, SENT = 1, OK = 2, FAILED = 3, CANCELLED = 4;
    private static final int BARS = 7;
    // The middle bars move most, as a voice meter does.
    private static final float[] SHAPE = {0.45f, 0.7f, 0.9f, 1f, 0.9f, 0.7f, 0.45f};
    private static final long GATHER_MS = 220, MARK_MS = 280, FALL_MS = 320;
    private static final int BLUE = 0xFF6EA8FF, GREEN = 0xFF5FD38D, AMBER = 0xFFFFB454, GREY = 0xFF9AA3B5;

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Runnable frame = this::invalidate;
    private final float dp;
    private final float[] heights = new float[BARS];    // as last drawn listening: where gathering or falling starts
    private volatile float target;                      // the mic's level, 0..1, from the recording thread
    private float shown;
    private int state = LISTENING, next = -1;           // next: the reply came during the gathering, shown after it
    private long since = SystemClock.uptimeMillis(), lastFrame;

    ListenIndicator(Context context) {
        super(context);
        dp = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, 1, getResources().getDisplayMetrics());
        paint.setStrokeCap(Paint.Cap.ROUND);
    }

    /** A 20 ms frame's level in dB, from the recording thread: read at the next frame drawn, nothing posted. */
    void level(double db) {
        target = (float) Math.max(0, Math.min(1, (db + 60) / 45));
    }

    /** Listening is over and the audio sent (or the laptop decided): gather, then dots until the reply. */
    void sent() {
        if (state == LISTENING) go(SENT);
    }

    void done(boolean ok) {
        if (state == SENT && SystemClock.uptimeMillis() - since < GATHER_MS) next = ok ? OK : FAILED;
        else go(ok ? OK : FAILED);
    }

    void cancelled() {
        go(CANCELLED);
    }

    private void go(int s) {
        state = s;
        next = -1;
        since = SystemClock.uptimeMillis();
        invalidate();
    }

    @Override
    protected void onDraw(Canvas c) {
        long now = SystemClock.uptimeMillis();
        float dt = lastFrame == 0 ? 0.016f : Math.min(0.1f, (now - lastFrame) / 1000f);
        lastFrame = now;
        if (state == SENT && next >= 0 && now - since >= GATHER_MS) {
            state = next;
            next = -1;
            since = now;
        }
        long t = now - since;
        float cx = getWidth() / 2f, cy = getHeight() / 2f, gap = 11 * dp;
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(5 * dp);
        boolean moving;
        switch (state) {
            case LISTENING: {
                // Fast rise, slower fall, per second rather than per frame: the TV draws at 24-60 Hz.
                float k = 1f - (float) Math.exp(-dt / (target > shown ? 0.04f : 0.2f));
                shown += (target - shown) * k;
                float secs = (now % 600_000) / 1000f, room = getHeight() - 5 * dp;
                paint.setColor(BLUE);
                for (int i = 0; i < BARS; i++) {
                    float voice = 0.55f + 0.25f * (float) Math.sin(secs * 6.1f + i * 1.7f)
                            + 0.2f * (float) Math.sin(secs * 9.3f + i * 2.9f);
                    // A slow ripple even in silence: it is listening.
                    float idle = 3 * dp * (0.5f + 0.5f * (float) Math.sin(secs * 2.6f - i * 0.7f));
                    heights[i] = Math.min(room, room * shown * SHAPE[i] * voice + idle);
                    bar(c, cx + (i - (BARS - 1) / 2f) * gap, cy, heights[i]);
                }
                moving = true;
                break;
            }
            case SENT:
                paint.setColor(BLUE);
                if (t < GATHER_MS) {
                    float e = ease((float) t / GATHER_MS);
                    for (int i = 0; i < BARS; i++) {
                        bar(c, cx + (i - (BARS - 1) / 2f) * gap * (1 - e), cy, heights[i] * (1 - e));
                    }
                } else {
                    // Working: three dots that brighten in turn, spreading out from the gathered one.
                    float w = (t - GATHER_MS) / 1000f, spread = Math.min(1f, w / 0.15f);
                    for (int i = -1; i <= 1; i++) {
                        float pulse = 0.5f + 0.5f * (float) Math.sin(w * 8.8f - (i + 1) * 0.9f);
                        paint.setAlpha((int) (110 + 145 * pulse));
                        paint.setStrokeWidth((5 + 1.5f * pulse) * dp);
                        c.drawPoint(cx + i * gap * spread, cy, paint);
                    }
                }
                moving = true;
                break;
            case OK:
            case FAILED: {
                float p = Math.min(1f, (float) t / MARK_MS), e = ease(p);
                int color = state == OK ? GREEN : AMBER;
                paint.setStyle(Paint.Style.FILL);
                paint.setColor(color);
                paint.setAlpha((int) (56 * e));
                c.drawCircle(cx, cy, (10 + 4 * e) * dp, paint);
                paint.setStyle(Paint.Style.STROKE);
                paint.setStrokeWidth(3 * dp);
                paint.setColor(color);
                if (state == OK) {
                    // The tick's short stroke, then its long one.
                    float ax = cx - 6 * dp, ay = cy, bx = cx - 1.5f * dp, by = cy + 4.5f * dp;
                    float dx = cx + 6.5f * dp, dy = cy - 5 * dp, split = 0.35f;
                    float f = Math.min(1f, e / split);
                    c.drawLine(ax, ay, ax + (bx - ax) * f, ay + (by - ay) * f, paint);
                    if (e > split) {
                        f = (e - split) / (1 - split);
                        c.drawLine(bx, by, bx + (dx - bx) * f, by + (dy - by) * f, paint);
                    }
                } else {
                    float h = 4.5f * dp * e;
                    c.drawLine(cx - h, cy - h, cx + h, cy + h, paint);
                    c.drawLine(cx - h, cy + h, cx + h, cy - h, paint);
                }
                moving = p < 1;
                break;
            }
            default: {      // CANCELLED: the bars drop and fade, unlike the gathering of a sent command
                float p = Math.min(1f, (float) t / FALL_MS), e = p * p;
                paint.setColor(GREY);
                paint.setAlpha((int) (255 * (1 - p)));
                for (int i = 0; i < BARS; i++) {
                    bar(c, cx + (i - (BARS - 1) / 2f) * gap, cy + 9 * dp * e, heights[i] * (1 - e));
                }
                moving = p < 1;
            }
        }
        // Drawn at the first frame 20 ms on: every other one at 60 Hz (30 fps), each one at 24 Hz.
        if (moving) postOnAnimationDelayed(frame, 20);
        else lastFrame = 0;
    }

    /** A vertical bar with round ends; at height 0 a dot. */
    private void bar(Canvas c, float x, float cy, float h) {
        if (h < 0.5f) c.drawPoint(x, cy, paint);
        else c.drawLine(x, cy - h / 2, x, cy + h / 2, paint);
    }

    private static float ease(float p) {
        return 1 - (1 - p) * (1 - p);
    }
}
