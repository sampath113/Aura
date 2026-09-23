package org.aura.pocket;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * AURA Pocket - a thin, offline-friendly client for the AURA assistant running
 * on your own computer.
 *
 * The app bundles one page: a connection screen. Once it reaches the AURA
 * server on your local network it loads that server's own web UI, so there is
 * exactly one interface to maintain and nothing is sent to the internet.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "aura";
    private static final String KEY_SERVER = "server";
    private static final String CONNECT_PAGE = "file:///android_asset/index.html";
    private static final int DEFAULT_PORT = 8765;

    private WebView web;
    private String pendingServer = "";
    private String lastError = "";

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        setContentView(web, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setMediaPlaybackRequiresUserGesture(false);

        web.addJavascriptInterface(new HostBridge(), "AuraHost");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return false;
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request == null || !request.isForMainFrame()) {
                    return;
                }
                lastError = String.valueOf(error.getDescription());
                loadConnectPage("Could not reach " + pendingServer + " (" + lastError + ").");
            }
        });

        String extra = (getIntent() == null) ? null : getIntent().getStringExtra("server");
        String target = (extra != null && !extra.trim().isEmpty())
                ? extra : prefs().getString(KEY_SERVER, "");
        if (target != null && !target.trim().isEmpty()) {
            loadServer(target);
        } else {
            loadConnectPage("");
        }
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, MODE_PRIVATE);
    }

    private void loadConnectPage(final String message) {
        pendingServer = "";
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                lastError = message == null ? "" : message;
                web.loadUrl(CONNECT_PAGE);
            }
        });
    }

    /** Save the address and load that server's UI. */
    private void loadServer(String rawAddress) {
        final String address = normalize(rawAddress);
        if (address.isEmpty()) {
            loadConnectPage("Enter the address shown by AURA on your computer.");
            return;
        }
        pendingServer = address;
        lastError = "";
        prefs().edit().putString(KEY_SERVER, address).apply();
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                web.loadUrl(address + "/");
            }
        });
    }

    /** Accept "192.168.1.5", "192.168.1.5:8765" or a full URL. */
    static String normalize(String raw) {
        String value = raw == null ? "" : raw.trim();
        if (value.isEmpty()) {
            return "";
        }
        if (!value.startsWith("http://") && !value.startsWith("https://")) {
            value = "http://" + value;
        }
        Uri uri = Uri.parse(value);
        String host = uri.getHost();
        if (host == null || host.isEmpty()) {
            return "";
        }
        int port = uri.getPort() > 0 ? uri.getPort() : DEFAULT_PORT;
        return uri.getScheme() + "://" + host + ":" + port;
    }

    public class HostBridge {

        @JavascriptInterface
        public String savedServer() {
            return prefs().getString(KEY_SERVER, "");
        }

        @JavascriptInterface
        public String lastError() {
            return lastError;
        }

        @JavascriptInterface
        public String version() {
            return "0.1.0";
        }

        @JavascriptInterface
        public void saveServer(String address) {
            prefs().edit().putString(KEY_SERVER, normalize(address)).apply();
        }

        @JavascriptInterface
        public void openServer(String address) {
            loadServer(address);
        }
    }

    @Override
    public void onBackPressed() {
        if (web != null && !CONNECT_PAGE.equals(web.getUrl())) {
            loadConnectPage("");
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
