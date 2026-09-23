package org.aura.app;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.Settings;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.ConsoleMessage;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.List;

/**
 * AURA, on a phone.
 *
 * The app is a shell around the same program that runs on a desktop: Python
 * (inside the APK, through Chaquopy) starts AURA's own HTTP server on
 * 127.0.0.1, and the WebView shows the very same interface the desktop build
 * shows. So there is one interface, one library format and one answer pipeline,
 * and this file only has to do the four things a phone adds:
 *
 * 1. show a first-run screen that explains what AURA needs and asks for access
 *    to the user's files (Android's "all files" permission - AURA indexes the
 *    documents the student already has, and that is the only way to read them);
 * 2. hand Python the paths it cannot work out for itself: the app's private data
 *    directory, where downloaded models should go by default, and the native
 *    library directory the bundled model engine runs from;
 * 3. be the file picker: a phone has nothing to paste a path into, so "add
 *    files" and "add a folder" go through the system picker, which JavaScript
 *    reaches through {@link Host.Bridge}; the model folder is chosen the same
 *    way from Settings;
 * 4. get out of the way - unload the model when the app leaves the screen, and
 *    stop the server when the app is closed, so a phone is never left holding a
 *    gigabyte of weights it is not using.
 *
 * No AndroidX and no XML layouts on purpose: the three screens are built in
 * code, so the whole app is four Java files, one manifest and one icon, and
 * every line of it can be read in one sitting.
 */
public class MainActivity extends Activity {

    private static final String TAG = "AURA";
    private static final String PREFS = "aura";
    private static final String KEY_SET_UP = "set_up";
    private static final long PICK_TIMEOUT_MS = 10L * 60L * 1000L;

    private static final int BG = 0xFF0B1020;
    private static final int PANEL = 0xFF121A33;
    private static final int LINE = 0xFF26325C;
    private static final int TEXT = 0xFFE8ECFB;
    private static final int MUTED = 0xFF93A0C8;
    private static final int BAD = 0xFFFF8F8F;
    private static final int ACCENT = 0xFF4D7FE0;
    private static final int ACCENT_TEXT = 0xFF05112B;

    private FrameLayout root;
    private ScrollView panel;
    private LinearLayout column;
    private WebView web;

    private boolean pythonStarted = false;
    private boolean closing = false;
    private boolean setupFolderPending = false;

    private Pickers.Request pendingPick;
    private boolean pendingFolder;
    private ValueCallback<Uri[]> fileCallback;

    // ------------------------------------------------------------------ life
    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        try {
            getWindow().setStatusBarColor(BG);
            getWindow().setNavigationBarColor(BG);
        } catch (Exception ignored) {
            // a device that will not colour its system bars is not a problem
        }
        Host.attach(this);
        buildShell();
        if (shouldShowSetup()) {
            showSetup();
        } else {
            load();
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        callPython("foreground");
    }

    @Override
    protected void onStop() {
        super.onStop();
        // The model itself is unloaded by Python a little later, so switching
        // away and straight back does not pay for a reload.
        callPython("background");
    }

    @Override
    protected void onDestroy() {
        closing = true;
        callPython("shutdown");
        Pickers.Request request = pendingPick;
        pendingPick = null;
        if (request != null) {
            request.done(null, "the app was closed");
        }
        ValueCallback<Uri[]> callback = fileCallback;
        fileCallback = null;
        if (callback != null) {
            callback.onReceiveValue(null);
        }
        Host.detach(this);
        if (web != null) {
            if (root != null) {
                root.removeView(web);
            }
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
            return;
        }
        super.onBackPressed();
    }

    private void buildShell() {
        root = new FrameLayout(this);
        root.setBackgroundColor(BG);
        column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        column.setGravity(Gravity.CENTER_HORIZONTAL);
        int pad = dp(22);
        column.setPadding(pad, pad + dp(24), pad, pad);
        panel = new ScrollView(this);
        panel.setBackgroundColor(BG);
        panel.addView(column, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(panel, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);
    }

    // -------------------------------------------------------------- first run
    private boolean shouldShowSetup() {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        if (!prefs.getBoolean(KEY_SET_UP, false)) {
            return true;
        }
        // Asked once already, but AURA still cannot see the user's files: one
        // tap per launch keeps the offer in reach.
        return !hasFileAccess();
    }

    private void showSetup() {
        final boolean access = hasFileAccess();
        fillScreen();
        column.addView(icon());
        column.addView(heading("AURA"));
        column.addView(paragraph("Everything runs on this phone. AURA indexes your PDFs, notes and "
                + "photos here, and every answer comes back with the page it came from. Nothing is "
                + "uploaded.", TEXT));
        column.addView(paragraph(access
                ? "AURA can read your files. Add them with the buttons in the Library panel - "
                  + "whole folders work too."
                : "To read the files you already have, Android has to be asked once. Without it you "
                  + "can still add files one at a time, but AURA cannot index a whole folder.",
                MUTED));
        column.addView(paragraph("Downloaded models go in " + modelsDir()
                + " by default - you can change that in Settings, at the model list.", MUTED));
        if (!access) {
            Button allow = tapButton("Allow access to my files", true);
            allow.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View view) {
                    requestFileAccess();
                }
            });
            column.addView(allow);
        }
        Button begin = tapButton(access ? "Start AURA" : "Continue without it", !access);
        begin.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                getSharedPreferences(PREFS, MODE_PRIVATE)
                        .edit().putBoolean(KEY_SET_UP, true).apply();
                load();
            }
        });
        column.addView(begin);
        panel.setVisibility(View.VISIBLE);
    }

    // ---------------------------------------------------------------- loading
    private void load() {
        fillScreen();
        column.addView(icon());
        column.addView(heading("AURA"));
        column.addView(paragraph("Starting AURA...", TEXT));
        ProgressBar spinner = new ProgressBar(this);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.topMargin = dp(16);
        spinner.setLayoutParams(params);
        column.addView(spinner);
        column.addView(paragraph("The first start also unpacks Python, so it can take a few "
                + "seconds.", MUTED));
        panel.setVisibility(View.VISIBLE);
        startPython();
    }

    private void startPython() {
        if (pythonStarted) {
            return;
        }
        pythonStarted = true;
        final String config = configJson();
        Thread thread = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    PyObject module = Python.getInstance().getModule("aura_mobile");
                    module.callAttr("main", config);
                } catch (Throwable error) {
                    Log.e(TAG, "Python failed to start", error);
                    Host.onError("AURA could not start: " + error);
                }
            }
        }, "aura-python");
        thread.start();
    }

    private void callPython(String function) {
        if (!pythonStarted) {
            return;
        }
        try {
            Python.getInstance().getModule("aura_mobile").callAttr(function);
        } catch (Throwable error) {
            Log.w(TAG, "aura_mobile." + function + " failed", error);
        }
    }

    // ------------------------------------------------------- calls from Python
    void serverReady(String url) {
        if (closing || url == null || url.isEmpty()) {
            return;
        }
        showWeb(url);
    }

    void serverFailed(String message) {
        if (closing) {
            return;
        }
        fillScreen();
        column.addView(icon());
        column.addView(heading("AURA could not start"));
        column.addView(paragraph(message == null ? "no reason was given" : message, BAD));
        column.addView(paragraph("Your documents and settings are safe - this is a problem with "
                + "starting AURA itself, not with your library.", MUTED));
        Button again = tapButton("Try again", true);
        again.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                pythonStarted = false;
                load();
            }
        });
        column.addView(again);
        Button close = tapButton("Close", false);
        close.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                closeApp();
            }
        });
        column.addView(close);
        panel.setVisibility(View.VISIBLE);
    }

    void serverStopped() {
        closeApp();
    }

    // ---------------------------------------------------------- the interface
    private void showWeb(String url) {
        if (web == null) {
            web = buildWebView();
            root.addView(web, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }
        panel.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(url);
    }

    private WebView buildWebView() {
        WebView view = new WebView(this);
        view.setBackgroundColor(BG);
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(false);
        settings.setMediaPlaybackRequiresUserGesture(false);
        // The server sends no-store as well; this makes sure an updated
        // interface is never a cached one.
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        view.addJavascriptInterface(new Host.Bridge(), "AndroidAura");
        view.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView target, WebResourceRequest request,
                                        WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    Host.onError("the AURA page could not be loaded ("
                            + (error == null ? "unknown reason" : error.getDescription()) + ")");
                }
            }
        });
        view.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView target, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileCallback != null) {
                    fileCallback.onReceiveValue(null);
                }
                fileCallback = callback;
                try {
                    startActivityForResult(Pickers.webFilesIntent(), Pickers.REQUEST_WEB_FILES);
                    return true;
                } catch (Exception error) {
                    Log.w(TAG, "no file chooser", error);
                    fileCallback = null;
                    return false;
                }
            }

            @Override
            public boolean onConsoleMessage(ConsoleMessage message) {
                Log.i(TAG, "page: " + message.message());
                return true;
            }
        });
        return view;
    }

    // ----------------------------------------------------------------- picking
    /**
     * Called from JavaScript through the bridge; blocks the calling thread until
     * the user has answered, or the timeout expires.
     */
    String pickSync(boolean folder) {
        Pickers.Request request = new Pickers.Request();
        Pickers.Request previous = pendingPick;
        pendingPick = request;
        pendingFolder = folder;
        if (previous != null) {
            previous.done(null, "another picker was opened");
        }
        startPicker(request, folder);
        request.await(PICK_TIMEOUT_MS);
        return request.json();
    }

    private void startPicker(final Pickers.Request request, final boolean folder) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                try {
                    startActivityForResult(folder ? Pickers.folderIntent() : Pickers.filesIntent(),
                            folder ? Pickers.REQUEST_FOLDER : Pickers.REQUEST_FILES);
                } catch (Exception error) {
                    Log.w(TAG, "no picker", error);
                    if (pendingPick == request) {
                        pendingPick = null;
                    }
                    request.done(null, "this device has no file picker (" + error + ")");
                }
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == Pickers.REQUEST_WEB_FILES) {
            ValueCallback<Uri[]> callback = fileCallback;
            fileCallback = null;
            if (callback != null) {
                List<Uri> chosen = resultCode == RESULT_OK ? Pickers.urisFrom(data, true)
                        : new ArrayList<Uri>();
                callback.onReceiveValue(chosen.isEmpty() ? null : chosen.toArray(new Uri[0]));
            }
            return;
        }
        if (requestCode == Pickers.REQUEST_FOLDER || requestCode == Pickers.REQUEST_FILES) {
            Pickers.Request request = pendingPick;
            pendingPick = null;
            if (request == null) {
                return;
            }
            if (resultCode != RESULT_OK || data == null) {
                request.done(null, null);
                return;
            }
            request.done(resolvePaths(data, requestCode == Pickers.REQUEST_FOLDER), null);
            return;
        }
        if (requestCode == Pickers.REQUEST_STORAGE) {
            onSetupStateChanged();
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    /**
     * Turn picker results into paths AURA can read.
     *
     * A folder is only ever a path (there is nothing sensible to copy), so a
     * folder that has no path is reported rather than silently ignored. A file
     * with no path - one that lives in a provider AURA cannot reach - is copied
     * into the app instead, which is why adding a file always works.
     */
    private List<String> resolvePaths(Intent data, boolean folder) {
        List<String> paths = new ArrayList<>();
        List<Uri> uris = Pickers.urisFrom(data, true);
        if (uris.isEmpty()) {
            return paths;
        }
        File incoming = new File(getFilesDir(), "imports");
        for (Uri uri : uris) {
            String path = Pickers.pathOf(uri);
            if (path.isEmpty() && !folder) {
                path = Pickers.copyInto(this, uri, incoming);
            }
            if (!path.isEmpty()) {
                paths.add(path);
            }
        }
        if (paths.isEmpty()) {
            toast(folder
                    ? "That folder has no path AURA can read. Choose one on internal storage."
                    : "That file could not be read. Try adding it again.");
        }
        return paths;
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] granted) {
        if (requestCode == Pickers.REQUEST_STORAGE) {
            onSetupStateChanged();
            return;
        }
        super.onRequestPermissionsResult(requestCode, permissions, granted);
    }

    private void onSetupStateChanged() {
        if (pythonStarted) {
            if (web != null) {
                // Access may have just been granted; let the page ask again.
                web.reload();
            }
            return;
        }
        if (shouldShowSetup()) {
            showSetup();
        } else {
            load();
        }
    }

    void requestFileAccess() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            Intent own = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
            own.setData(Uri.parse("package:" + getPackageName()));
            if (openSafely(own)) {
                return;
            }
            if (openSafely(new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))) {
                return;
            }
        }
        requestPermissions(new String[]{"android.permission.READ_EXTERNAL_STORAGE"},
                Pickers.REQUEST_STORAGE);
    }

    private boolean openSafely(Intent intent) {
        try {
            startActivityForResult(intent, Pickers.REQUEST_STORAGE);
            return true;
        } catch (Exception error) {
            Log.w(TAG, "cannot open " + intent.getAction(), error);
            return false;
        }
    }

    // ------------------------------------------------------------------- paths
    boolean hasFileAccess() {
        return Pickers.hasFileAccess(this);
    }

    String dataDir() {
        File dir = new File(getFilesDir(), "library");
        dir.mkdirs();
        return dir.getAbsolutePath();
    }

    /**
     * Where shared, user-visible files live.
     *
     * Internal storage when the user gave AURA access to it, and the app's own
     * external directory otherwise - never a path that cannot be written to,
     * because Python turns this into the default folder for downloaded models.
     */
    String storageRoot() {
        File shared = new File(Environment.getExternalStorageDirectory(), "AURA");
        if (hasFileAccess() || shared.isDirectory() || shared.mkdirs()) {
            return shared.getAbsolutePath();
        }
        File external = getExternalFilesDir(null);
        if (external != null) {
            File own = new File(external, "AURA");
            if (own.isDirectory() || own.mkdirs()) {
                return own.getAbsolutePath();
            }
        }
        File internal = new File(getFilesDir(), "storage");
        internal.mkdirs();
        return internal.getAbsolutePath();
    }

    String modelsDir() {
        return new File(storageRoot(), "models").getAbsolutePath();
    }

    String appVersion() {
        try {
            PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            return info.versionName == null ? "" : info.versionName;
        } catch (Exception error) {
            return "";
        }
    }

    /**
     * Everything Python cannot work out for itself, as one JSON object.
     *
     * A phone has no home directory worth using and no way to guess where the
     * user's files are; these values are what make AURA behave on a phone
     * exactly as it does on a desktop.
     */
    private String configJson() {
        JSONObject json = new JSONObject();
        try {
            json.put("data_dir", dataDir());
            json.put("models_dir", modelsDir());
            json.put("storage_root", storageRoot());
            json.put("native_lib_dir", getApplicationInfo().nativeLibraryDir);
            json.put("permission", hasFileAccess() ? "all" : "app");
            json.put("api_level", Build.VERSION.SDK_INT);
            json.put("app_version", appVersion());
        } catch (Exception error) {
            Log.w(TAG, "config", error);
        }
        return json.toString();
    }

    void closeApp() {
        closing = true;
        try {
            finishAndRemoveTask();
        } catch (Exception error) {
            finish();
        }
    }

    // ------------------------------------------------------------------ screens
    private void fillScreen() {
        if (column == null) {
            buildShell();
        }
        column.removeAllViews();
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    private View icon() {
        ImageView view = new ImageView(this);
        view.setImageResource(R.drawable.ic_aura);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(dp(84), dp(84));
        params.bottomMargin = dp(14);
        view.setLayoutParams(params);
        return view;
    }

    private TextView heading(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(TEXT);
        view.setTextSize(22);
        view.setTypeface(Typeface.DEFAULT_BOLD);
        view.setGravity(Gravity.CENTER);
        return view;
    }

    private TextView paragraph(String text, int color) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(color);
        view.setTextSize(14);
        view.setGravity(Gravity.CENTER);
        view.setPadding(0, dp(4), 0, dp(4));
        return view;
    }

    private Button tapButton(String label, boolean primary) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextSize(15);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.topMargin = dp(10);
        button.setLayoutParams(params);
        if (primary) {
            GradientDrawable shape = new GradientDrawable();
            shape.setColor(ACCENT);
            shape.setCornerRadius(dp(12));
            button.setBackground(shape);
            button.setTextColor(ACCENT_TEXT);
        } else {
            GradientDrawable shape = new GradientDrawable();
            shape.setColor(PANEL);
            shape.setCornerRadius(dp(12));
            shape.setStroke(dp(1), LINE);
            button.setBackground(shape);
            button.setTextColor(TEXT);
        }
        return button;
    }

    private int dp(float value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
