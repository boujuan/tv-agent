package com.boujuan.tvagent;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.StateListDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.TextUtils;
import android.text.style.ForegroundColorSpan;
import android.text.style.StyleSpan;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * A numbered list from tv-agent ("what was I watching?", "what's new?"), opened by the laptop over adb with the list
 * as JSON. Say "number 2" (the pill opens on top; the list stays visible behind it) or pick with the remote: OK on a
 * row, or a number key. Closes when playback takes the screen, on BACK, or after 90 s untouched. Sized for six rows
 * on the TCL's 960×540 dp screen. In help mode ("what can you do?") it lists every command by section instead, as
 * patterns with their ‹placeholders› set apart, in the language it was asked in: nothing to pick, ▲▼ scrolls.
 */
public class ChoicesActivity extends Activity {
    private static final long IDLE_CLOSE_MS = 90_000;
    /** A list is on screen: the pill then opens bottom-right, beside it instead of over it. */
    static volatile boolean showing;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService net = Executors.newFixedThreadPool(3);
    private final Runnable idleClose = this::finish;
    private final List<TextView> details = new ArrayList<>();
    private LinearLayout rows;
    private ScrollView scroll;
    private LinearLayout panel;
    private TextView title, legend, hint;
    private boolean help;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        buildUi();
        if (!show(getIntent())) {
            finish();
            return;
        }
        showing = true;
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (!show(intent)) finish();     // a new list replaces this one; "close" ends it
    }

    @Override
    protected void onStop() {
        super.onStop();
        finish();                        // covered by the player (or Home): the list has done its job
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        showing = false;
        ui.removeCallbacks(idleClose);
        net.shutdownNow();
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent event) {
        ui.removeCallbacks(idleClose);
        ui.postDelayed(idleClose, IDLE_CLOSE_MS);
        return super.dispatchKeyEvent(event);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode >= KeyEvent.KEYCODE_1 && keyCode <= KeyEvent.KEYCODE_9 && !help) {
            int n = keyCode - KeyEvent.KEYCODE_0;
            if (n <= rows.getChildCount()) choose(n);
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            finish();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private int dp(float v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private void buildUi() {
        FrameLayout root = new FrameLayout(this);
        panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(20), dp(14), dp(20), dp(12));
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0xF0141820);
        bg.setCornerRadius(dp(24));
        bg.setStroke(dp(1), 0x33FFFFFF);
        panel.setBackground(bg);

        title = new TextView(this);
        title.setTextColor(Color.WHITE);
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
        title.setPadding(dp(8), 0, 0, dp(6));
        legend = new TextView(this);
        legend.setTextColor(0x99FFFFFF);
        legend.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        legend.setPadding(dp(8), 0, 0, dp(4));
        rows = new LinearLayout(this);
        rows.setOrientation(LinearLayout.VERTICAL);
        scroll = new ScrollView(this);
        scroll.setVerticalScrollBarEnabled(true);
        scroll.addView(rows);
        hint = new TextView(this);
        hint.setTextColor(0x99FFFFFF);
        hint.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        hint.setPadding(dp(8), dp(6), 0, 0);

        panel.addView(title);
        panel.addView(legend);
        panel.addView(scroll);
        panel.addView(hint);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(dp(500), FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.START | Gravity.CENTER_VERTICAL);
        lp.leftMargin = dp(48);
        root.addView(panel, lp);
        setContentView(root);
    }

    /**
     * Fills the list from the intent's JSON: {"title": …, "items": [{"label", "detail", "poster"}, …]}; with
     * "mode": "help" ("legend", "hint" in the language asked in), items marked "header" are section titles, the
     * others a pattern and what it does, and nothing is numbered or picked.
     */
    private boolean show(Intent intent) {
        if (intent.getBooleanExtra("close", false)) return false;
        String data = intent.getStringExtra("list");
        if (data == null) return false;
        try {
            JSONObject list = new JSONObject(data);
            JSONArray items = list.getJSONArray("items");
            if (items.length() == 0) return false;
            title.setText(list.optString("title", ""));
            help = "help".equals(list.optString("mode"));
            rows.removeAllViews();
            details.clear();
            for (int i = 0; i < (help ? items.length() : Math.min(items.length(), 6)); i++) {
                JSONObject it = items.getJSONObject(i);
                if (help) {
                    rows.addView(it.optBoolean("header") ? header(it.optString("label"))
                            : helpRow(it.optString("label"), it.optString("detail")));
                } else {
                    rows.addView(row(i + 1, it.optString("label"), it.optString("detail"), it.optString("poster")));
                }
            }
            // Six picks fit as they are; the help list scrolls in a fixed height, wider for its two columns.
            scroll.setLayoutParams(new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                    help ? dp(380) : LinearLayout.LayoutParams.WRAP_CONTENT));
            FrameLayout.LayoutParams plp = (FrameLayout.LayoutParams) panel.getLayoutParams();
            plp.width = dp(help ? 560 : 500);
            panel.setLayoutParams(plp);
            legend.setText(list.optString("legend", ""));
            legend.setVisibility(help && !list.optString("legend").isEmpty() ? View.VISIBLE : View.GONE);
            hint.setText(list.optString("hint", help ? "Press the mic and say one · ▲▼ to scroll · BACK closes"
                    : "Say “number 2”, or pick with OK or a number key"));
            for (int i = 0; i < rows.getChildCount(); i++) {
                if (rows.getChildAt(i).isFocusable()) {
                    rows.getChildAt(i).requestFocus();
                    break;
                }
            }
            ui.removeCallbacks(idleClose);
            ui.postDelayed(idleClose, IDLE_CLOSE_MS);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private View row(int n, String label, String detail, String poster) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(6), dp(4), dp(10), dp(4));
        row.setFocusable(true);
        row.setClickable(true);
        GradientDrawable focused = new GradientDrawable();
        focused.setColor(0x336EA8FF);
        focused.setStroke(dp(2), 0xFF6EA8FF);
        focused.setCornerRadius(dp(14));
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[]{android.R.attr.state_focused}, focused);
        states.addState(new int[]{}, new GradientDrawable());
        row.setBackground(states);
        row.setOnClickListener(v -> choose(n));

        TextView number = new TextView(this);
        number.setText(String.valueOf(n));
        number.setTextColor(0xFF6EA8FF);
        number.setTextSize(TypedValue.COMPLEX_UNIT_SP, 20);
        number.setGravity(Gravity.CENTER);
        row.addView(number, new LinearLayout.LayoutParams(dp(32), LinearLayout.LayoutParams.WRAP_CONTENT));

        ImageView image = new ImageView(this);
        image.setScaleType(ImageView.ScaleType.CENTER_CROP);
        GradientDrawable frame = new GradientDrawable();
        frame.setColor(0x26FFFFFF);
        frame.setCornerRadius(dp(6));
        image.setBackground(frame);
        image.setClipToOutline(true);
        LinearLayout.LayoutParams ip = new LinearLayout.LayoutParams(dp(36), dp(54));
        ip.rightMargin = dp(12);
        row.addView(image, ip);
        loadPoster(image, poster);

        LinearLayout texts = new LinearLayout(this);
        texts.setOrientation(LinearLayout.VERTICAL);
        TextView main = new TextView(this);
        main.setText(label);
        main.setTextColor(Color.WHITE);
        main.setTextSize(TypedValue.COMPLEX_UNIT_SP, 17);
        main.setSingleLine(true);
        main.setEllipsize(TextUtils.TruncateAt.END);
        TextView sub = new TextView(this);
        sub.setText(detail);
        sub.setTextColor(0xB3FFFFFF);
        sub.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        sub.setSingleLine(true);
        sub.setEllipsize(TextUtils.TruncateAt.END);
        texts.addView(main);
        texts.addView(sub);
        details.add(sub);
        row.addView(texts, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));

        LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        rp.bottomMargin = dp(2);
        row.setLayoutParams(rp);
        return row;
    }

    private View header(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextColor(0xFF6EA8FF);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        t.setPadding(dp(8), dp(10), dp(8), dp(2));
        return t;
    }

    /** A pattern with its ‹placeholders› in blue italics, and its [optional] parts and “·” separators dimmed. */
    private static CharSequence pattern(String text) {
        SpannableStringBuilder b = new SpannableStringBuilder(text);
        Matcher m = Pattern.compile("\\[[^\\]]*\\]| · ").matcher(text);
        while (m.find()) {
            b.setSpan(new ForegroundColorSpan(0x99FFFFFF), m.start(), m.end(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        m = Pattern.compile("‹[^›]+›").matcher(text);
        while (m.find()) {
            b.setSpan(new ForegroundColorSpan(0xFF8EC1FF), m.start(), m.end(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            b.setSpan(new StyleSpan(Typeface.ITALIC), m.start(), m.end(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        }
        return b;
    }

    /** A help line, the pattern beside what it does: focusable only so ▲▼ scrolls through them. */
    private View helpRow(String label, String detail) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(8), dp(4), dp(8), dp(4));
        row.setFocusable(true);
        GradientDrawable focused = new GradientDrawable();
        focused.setColor(0x226EA8FF);
        focused.setCornerRadius(dp(10));
        StateListDrawable states = new StateListDrawable();
        states.addState(new int[]{android.R.attr.state_focused}, focused);
        states.addState(new int[]{}, new GradientDrawable());
        row.setBackground(states);
        TextView main = new TextView(this);
        main.setText(pattern(label));
        main.setTextColor(Color.WHITE);
        main.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        TextView sub = new TextView(this);
        sub.setText(detail);
        sub.setTextColor(0x99FFFFFF);
        sub.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        row.addView(main, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.25f));
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        sp.leftMargin = dp(14);
        row.addView(sub, sp);
        return row;
    }

    private void loadPoster(ImageView image, String url) {
        if (!(url.startsWith("https://") || url.startsWith("http://"))) return;
        net.execute(() -> {
            HttpURLConnection c = null;
            try {
                c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(3000);
                c.setReadTimeout(5000);
                c.setInstanceFollowRedirects(true);
                BitmapFactory.Options o = new BitmapFactory.Options();
                o.inPreferredConfig = Bitmap.Config.RGB_565;
                Bitmap b;
                try (InputStream in = c.getInputStream()) {
                    b = BitmapFactory.decodeStream(in, null, o);
                }
                if (b != null) ui.post(() -> image.setImageBitmap(b));
            } catch (Exception ignored) {
                // no poster: the row still works
            } finally {
                if (c != null) c.disconnect();
            }
        });
    }

    /** Tells tv-agent which item was picked; it plays it, and the player covering the list closes it. */
    private void choose(int n) {
        if (help || n > details.size()) return;
        details.get(n - 1).setText("Starting…");
        String base = Pairing.url(this), token = Pairing.token(this);
        net.execute(() -> {
            boolean ok = post(base, token, "/v1/choose", ("{\"number\":" + n + "}").getBytes(StandardCharsets.UTF_8));
            ui.post(() -> {
                if (!ok) hint.setText("tv-agent didn't answer: is the laptop awake?");
            });
        });
    }

    private static boolean post(String base, String token, String path, byte[] body) {
        if (base.isEmpty()) return false;
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + path).openConnection();
            c.setConnectTimeout(1500);
            c.setReadTimeout(4000);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("Authorization", "Bearer " + token);
            c.setRequestProperty("Content-Type", "application/json");
            c.setFixedLengthStreamingMode(body.length);
            try (OutputStream o = c.getOutputStream()) {
                o.write(body);
            }
            return c.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }
}
