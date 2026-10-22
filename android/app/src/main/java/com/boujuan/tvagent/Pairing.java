package com.boujuan.tvagent;

import android.content.Context;
import android.content.SharedPreferences;

/** Where tv-agent runs and the token it expects, kept in the app's private settings. */
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

    static boolean save(Context c, String url, String token) {
        if (url == null || token == null || token.length() < 16
                || !(url.startsWith("http://") || url.startsWith("https://"))) {
            return false;
        }
        return prefs(c).edit().putString("url", url.replaceAll("/+$", "")).putString("token", token).commit();
    }
}
