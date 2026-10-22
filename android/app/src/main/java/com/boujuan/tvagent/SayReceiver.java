package com.boujuan.tvagent;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.widget.Toast;

/** Shows the laptop's short messages ("Playing The Bear · S3E2 · 4K") as a toast. Only shell can send them. */
public class SayReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String text = intent.getStringExtra("text");
        if (text == null || text.isEmpty()) return;
        if (text.length() > 120) text = text.substring(0, 120);
        Toast.makeText(context, text, Toast.LENGTH_LONG).show();
    }
}
