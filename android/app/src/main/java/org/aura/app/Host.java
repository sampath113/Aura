package org.aura.app;

import android.os.Build;
import android.webkit.JavascriptInterface;

import org.json.JSONObject;

/**
 * The one thin line between the app and AURA's Python.
 *
 * Two directions, and nothing else:
 *
 * - Python (Chaquopy) calls the static methods to say the server is up, to
 *   report a failure, or to say it has stopped, so the app can swap its loading
 *   screen for the interface instead of showing a spinner for ever;
 * - JavaScript calls {@link Bridge}, which the activity installs on the WebView
 *   as {@code AndroidAura}, to use the system file picker - the single thing the
 *   page cannot do for itself.
 *
 * The bridge deliberately has no generic "make this HTTP request" method: the
 * page talks to AURA over ordinary HTTP to 127.0.0.1, exactly as it does on a
 * desktop, and only the picker needs a detour through Java.
 */
public final class Host {

    private static MainActivity activity;

    private Host() {
    }

    static void attach(MainActivity value) {
        activity = value;
    }

    static void detach(MainActivity value) {
        if (activity == value) {
            activity = null;
        }
    }

    // ------------------------------------------------- called from Python
    public static void onReady(final String url) {
        onUi(new Runnable() {
            @Override
            public void run() {
                MainActivity current = activity;
                if (current != null) {
                    current.serverReady(url);
                }
            }
        });
    }

    public static void onError(final String message) {
        onUi(new Runnable() {
            @Override
            public void run() {
                MainActivity current = activity;
                if (current != null) {
                    current.serverFailed(message);
                }
            }
        });
    }

    public static void onStopped() {
        onUi(new Runnable() {
            @Override
            public void run() {
                MainActivity current = activity;
                if (current != null) {
                    current.serverStopped();
                }
            }
        });
    }

    private static void onUi(Runnable work) {
        MainActivity current = activity;
        if (current == null) {
            return;
        }
        current.runOnUiThread(work);
    }

    // ------------------------------------------------- called from JavaScript
    /** What the page is shown on. Installed as {@code AndroidAura}. */
    public static final class Bridge {

        /** Where this is running, so the interface can say "this phone". */
        @JavascriptInterface
        public String platform() {
            return platformJson();
        }

        /**
         * {@code "files"} or {@code "folder"} -> {"paths": [...], "error": ...}
         *
         * Blocks until the user has answered, because JavaScript has to be given
         * a value; a timeout stops a page freezing for ever if the picker is
         * never answered.
         */
        @JavascriptInterface
        public String pick(String kind) {
            MainActivity current = activity;
            if (current == null) {
                return "{\"error\":\"the app is not ready yet\"}";
            }
            return current.pickSync("folder".equals(kind));
        }

        /** Open the system page where "all files" access is granted. */
        @JavascriptInterface
        public void openStorageSettings() {
            Host.openStorageSettings();
        }

        /** Close the app (the interface has a Quit button). */
        @JavascriptInterface
        public void quit() {
            Host.quit();
        }
    }

    public static String platformJson() {
        MainActivity current = activity;
        JSONObject json = new JSONObject();
        try {
            json.put("android", true);
            json.put("app_version", current == null ? "" : current.appVersion());
            json.put("api_level", Build.VERSION.SDK_INT);
            json.put("storage_root", current == null ? "" : current.storageRoot());
            json.put("models_dir", current == null ? "" : current.modelsDir());
            json.put("can_read_files", current != null && current.hasFileAccess());
        } catch (Exception ignored) {
            return "{\"android\":true}";
        }
        return json.toString();
    }

    public static void openStorageSettings() {
        final MainActivity current = activity;
        if (current == null) {
            return;
        }
        current.runOnUiThread(new Runnable() {
            @Override
            public void run() {
                current.requestFileAccess();
            }
        });
    }

    public static void quit() {
        final MainActivity current = activity;
        if (current == null) {
            return;
        }
        current.runOnUiThread(new Runnable() {
            @Override
            public void run() {
                current.closeApp();
            }
        });
    }
}
