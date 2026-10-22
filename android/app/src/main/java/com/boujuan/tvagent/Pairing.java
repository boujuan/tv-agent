package com.boujuan.tvagent;

import android.content.Context;
import android.content.SharedPreferences;

/** Where tv-agent runs, the token it expects and its audio-stream port, kept in the app's private settings. */
final class Pairing {
    private static final String FILE = "pairing";

    private Pairing() {
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    static String url(Context c) {
        return prefs(c).getString("url", "");
    }

    static String token(Context c) {
        return prefs(c).getString("token", "");
    }

    /** The port for streaming audio while you speak; 0 = only the older send-at-the-end request. */
    static int streamPort(Context c) {
        return prefs(c).getInt("stream_port", 0);
    }

    static boolean save(Context c, String url, String token, int streamPort) {
        if (url == null || token == null || token.length() < 16
                || !(url.startsWith("http://") || url.startsWith("https://"))) {
            return false;
        }
        return prefs(c).edit().putString("url", url.replaceAll("/+$", "")).putString("token", token)
                .putInt("stream_port", streamPort > 0 && streamPort < 65536 ? streamPort : 0).commit();
    }
}
