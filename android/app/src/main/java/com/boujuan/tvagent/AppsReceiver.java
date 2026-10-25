package com.boujuan.tvagent;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.Drawable;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * The TV's launchable apps for the agent (its app list, for voice and the phone's remote): their names and icons,
 * which ADB can't read. Writes apps.json ([{"package", "activity", "label", "icon"}]) and one PNG per app to
 * files/apps/ for the computer to read over ADB, then answers RESULT_OK with how many. Only shell can send it (the
 * manifest requires DUMP), like the other receivers.
 */
public class AppsReceiver extends BroadcastReceiver {
    private static final int ICON = 96;     // px: a phone list's icon

    @Override
    public void onReceive(Context context, Intent intent) {
        PendingResult result = goAsync();
        Context app = context.getApplicationContext();
        new Thread(() -> {
            try {
                result.setResultData(String.valueOf(write(app)));
                result.setResultCode(Activity.RESULT_OK);
            } catch (Exception e) {
                result.setResultData(e.getClass().getSimpleName());
                result.setResultCode(Activity.RESULT_CANCELED);
            } finally {
                result.finish();
            }
        }).start();
    }

    /** The apps the TV's home screen lists (MAIN + LEANBACK_LAUNCHER), one entry per package. */
    static int write(Context context) throws Exception {
        PackageManager pm = context.getPackageManager();
        File dir = new File(context.getExternalFilesDir(null), "apps");
        if (!dir.isDirectory() && !dir.mkdirs()) throw new IOException("no folder " + dir);
        File[] old = dir.listFiles();
        if (old != null) for (File f : old) if (f.getName().endsWith(".png")) f.delete();

        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LEANBACK_LAUNCHER);
        List<ResolveInfo> found = pm.queryIntentActivities(launcher, 0);
        JSONArray apps = new JSONArray();
        java.util.Set<String> seen = new java.util.HashSet<>();
        for (ResolveInfo r : found) {
            ActivityInfo a = r.activityInfo;
            if (a == null || !seen.add(a.packageName)) continue;
            JSONObject o = new JSONObject();
            o.put("package", a.packageName);
            o.put("activity", a.name);
            o.put("label", String.valueOf(r.loadLabel(pm)));
            String icon = a.packageName + ".png";
            if (png(iconOf(pm, a), new File(dir, icon))) o.put("icon", icon);
            apps.put(o);
        }
        File tmp = new File(dir, "apps.json.tmp");
        try (OutputStream out = new FileOutputStream(tmp)) {
            out.write(apps.toString().getBytes(StandardCharsets.UTF_8));
        }
        if (!tmp.renameTo(new File(dir, "apps.json"))) throw new IOException("apps.json not written");
        return apps.length();
    }

    /** Its icon; the banner when it has no icon of its own (some TV-only apps). */
    private static Drawable iconOf(PackageManager pm, ActivityInfo a) {
        if (a.getIconResource() == 0 && a.applicationInfo != null && a.applicationInfo.icon == 0) {
            Drawable banner = a.loadBanner(pm);
            if (banner != null) return banner;
        }
        return a.loadIcon(pm);
    }

    private static boolean png(Drawable d, File to) {
        if (d == null) return false;
        Bitmap bitmap = Bitmap.createBitmap(ICON, ICON, Bitmap.Config.ARGB_8888);
        int w = Math.max(1, d.getIntrinsicWidth()), h = Math.max(1, d.getIntrinsicHeight());
        float scale = Math.min((float) ICON / w, (float) ICON / h);       // a banner keeps its shape, centred
        int dw = Math.round(w * scale), dh = Math.round(h * scale);
        int left = (ICON - dw) / 2, top = (ICON - dh) / 2;
        d.setBounds(left, top, left + dw, top + dh);
        d.draw(new Canvas(bitmap));
        try (OutputStream out = new FileOutputStream(to)) {
            return bitmap.compress(Bitmap.CompressFormat.PNG, 100, out);
        } catch (IOException e) {
            return false;
        } finally {
            bitmap.recycle();
        }
    }
}
