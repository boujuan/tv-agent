package com.boujuan.tvagent;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Pairing from the computer: `tv-agent tv pair` sends the agent's address and token here over ADB. Only shell
 * can send it (the manifest requires DUMP). Answers RESULT_OK (-1) when saved, so the computer can check.
 */
public class ConfigReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        boolean ok = Pairing.save(context, intent.getStringExtra("url"), intent.getStringExtra("token"));
        setResultCode(ok ? Activity.RESULT_OK : Activity.RESULT_CANCELED);
    }
}
