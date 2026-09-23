package org.aura.app;

import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * The system file and folder pickers, and the awkward part of using them.
 *
 * AURA indexes files by path, and Android's pickers hand back content:// URIs.
 * For the folders the user actually uses (internal storage, an SD card) the URI
 * carries the real path inside its document id, so {@link #pathOf} digs it out.
 * For anything else - a document provider that keeps its files somewhere with
 * no path at all - {@link #copyInto} takes a copy into the app instead, so
 * "add this file" always works, even if AURA has no access to the user's
 * storage.
 *
 * Every method here is deliberately independent of the activity that calls it,
 * so the path arithmetic can be reasoned about (and checked) on its own.
 */
final class Pickers {

    static final int REQUEST_FILES = 5001;
    static final int REQUEST_FOLDER = 5002;
    static final int REQUEST_WEB_FILES = 5003;
    static final int REQUEST_STORAGE = 5004;

    private Pickers() {
    }

    // ------------------------------------------------------------------ intents
    /** "Add documents": any number of files of any type. */
    static Intent filesIntent() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        return intent;
    }

    /** "Add a folder", or "where should models go": one tree. */
    static Intent folderIntent() {
        return new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
    }

    /**
     * What the WebView needs when the page uses <code>&lt;input type="file"&gt;</code>.
     * It is a different intent, because the result has to go back to Chromium
     * rather than to us.
     */
    static Intent webFilesIntent() {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        return intent;
    }

    /** Every URI a picker returned, whether one was chosen or several. */
    static List<Uri> urisFrom(Intent data, boolean keepEmpty) {
        List<Uri> found = new ArrayList<>();
        if (data == null) {
            return found;
        }
        if (data.getClipData() != null) {
            for (int index = 0; index < data.getClipData().getItemCount(); index++) {
                Uri uri = data.getClipData().getItemAt(index).getUri();
                if (uri != null) {
                    found.add(uri);
                }
            }
        } else if (data.getData() != null) {
            found.add(data.getData());
        } else if (!keepEmpty) {
            found.clear();
        }
        return found;
    }

    // ------------------------------------------------------------------- paths
    /**
     * The real filesystem path behind a picker URI, or "" when there is none.
     *
     * Internal storage arrives as document id "primary:Download/notes.pdf" and
     * an SD card as "1A2B-3C4D:Books/x.pdf"; both convert back to a path that
     * AURA can read. A provider that does not work this way (the Downloads
     * provider's "msf:1000000032", cloud storage) returns "", which is the
     * caller's signal to copy the file instead.
     */
    static String pathOf(Uri uri) {
        if (uri == null) {
            return "";
        }
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.US);
        if ("file".equals(scheme)) {
            return uri.getPath() == null ? "" : uri.getPath();
        }
        if (!"content".equals(scheme)) {
            return "";
        }
        String documentId = "";
        try {
            List<String> segments = uri.getPathSegments();
            String first = segments.isEmpty() ? "" : segments.get(0);
            if (segments.size() >= 2 && ("tree".equals(first) || "document".equals(first))) {
                documentId = segments.get(1);
            } else {
                documentId = DocumentsContract.getDocumentId(uri);
            }
        } catch (Exception ignored) {
            return "";
        }
        return pathFromDocumentId(documentId);
    }

    /** "primary:Download/x.pdf" -> /storage/emulated/0/Download/x.pdf */
    static String pathFromDocumentId(String documentId) {
        if (documentId == null || documentId.isEmpty()) {
            return "";
        }
        if (documentId.startsWith("raw:")) {
            return documentId.substring("raw:".length());
        }
        int colon = documentId.indexOf(':');
        if (colon <= 0 || colon == documentId.length() - 1) {
            return "";
        }
        String volume = documentId.substring(0, colon);
        String rest = documentId.substring(colon + 1);
        if ("primary".equalsIgnoreCase(volume)) {
            File root = Environment.getExternalStorageDirectory();
            return root.getAbsolutePath() + "/" + rest;
        }
        // Removable volumes are addressed by their UUID, e.g. "1A2B-3C4D".
        if (volume.matches("(?i)^[0-9a-f]{4}-[0-9a-f]{4}$")) {
            return "/storage/" + volume + "/" + rest;
        }
        return "";
    }

    // ------------------------------------------------------------------ copying
    /**
     * Copy a picked file into the app's own storage and return the new path.
     *
     * The fallback for URIs with no usable path. It also means a file the user
     * picked is still readable after the picker's permission expires, which is
     * exactly what a library that can be asked about later needs.
     */
    static String copyInto(Context context, Uri uri, File folder) {
        if (context == null || uri == null) {
            return "";
        }
        String name = displayName(context, uri);
        if (name.isEmpty()) {
            name = "document";
        }
        folder.mkdirs();
        File target = new File(folder, name);
        int suffix = 1;
        while (target.exists()) {
            target = new File(folder, suffix + "-" + name);
            suffix++;
            if (suffix > 999) {
                break;
            }
        }
        InputStream input = null;
        OutputStream output = null;
        try {
            input = context.getContentResolver().openInputStream(uri);
            if (input == null) {
                return "";
            }
            output = new FileOutputStream(target);
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = input.read(buffer)) > 0) {
                output.write(buffer, 0, read);
            }
        } catch (Exception error) {
            return "";
        } finally {
            close(input);
            close(output);
        }
        return target.getAbsolutePath();
    }

    /** The name the user sees for a document URI (never a path). */
    static String displayName(Context context, Uri uri) {
        Cursor cursor = null;
        try {
            cursor = context.getContentResolver()
                    .query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null);
            if (cursor != null && cursor.moveToFirst()) {
                int column = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (column >= 0) {
                    String value = cursor.getString(column);
                    if (value != null && !value.isEmpty()) {
                        return safeName(value);
                    }
                }
            }
        } catch (Exception ignored) {
            // fall through to the last path segment
        } finally {
            close(cursor);
        }
        String last = uri.getLastPathSegment();
        return last == null ? "" : safeName(last);
    }

    /** Keep the characters a filesystem is happy with, and nothing else. */
    static String safeName(String raw) {
        StringBuilder clean = new StringBuilder();
        for (char character : raw.toCharArray()) {
            if (Character.isLetterOrDigit(character) || character == '.' || character == '-'
                    || character == '_' || character == ' ' || character == '('
                    || character == ')') {
                clean.append(character);
            } else {
                clean.append('_');
            }
        }
        String value = clean.toString().trim();
        if (value.isEmpty() || value.replace(".", "").isEmpty()) {
            return "document";
        }
        return value;
    }

    private static void close(Cursor cursor) {
        if (cursor != null) {
            try {
                cursor.close();
            } catch (Exception ignored) {
                // nothing useful to do
            }
        }
    }

    private static void close(java.io.Closeable stream) {
        if (stream != null) {
            try {
                stream.close();
            } catch (Exception ignored) {
                // nothing useful to do
            }
        }
    }

    // ------------------------------------------------------------------ needs
    /** The app-level "all files" access, in the way this Android version asks. */
    static boolean hasFileAccess(Context context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            try {
                return Environment.isExternalStorageManager();
            } catch (Exception ignored) {
                return false;
            }
        }
        return context.checkSelfPermission("android.permission.READ_EXTERNAL_STORAGE")
                == android.content.pm.PackageManager.PERMISSION_GRANTED;
    }

    /**
     * One picker, in progress.
     *
     * The interface calls {@code AndroidAura.pick()} from JavaScript, which has
     * to answer immediately - so the calling thread waits on this latch while
     * the activity shows the picker. A timeout matters: without one a picker
     * the user never answers would freeze the page for good.
     */
    static final class Request {
        private final CountDownLatch latch = new CountDownLatch(1);
        private final List<String> paths = new ArrayList<>();
        private String error = "";
        private boolean answered = false;

        synchronized void done(List<String> found, String problem) {
            if (answered) {
                return;
            }
            answered = true;
            if (found != null) {
                paths.addAll(found);
            }
            error = problem == null ? "" : problem;
            latch.countDown();
        }

        void await(long milliseconds) {
            try {
                latch.await(milliseconds, TimeUnit.MILLISECONDS);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
        }

        synchronized String json() {
            JSONObject out = new JSONObject();
            try {
                String problem = error;
                if (!answered && problem.isEmpty()) {
                    problem = "the file picker did not answer - try again";
                }
                if (!problem.isEmpty()) {
                    out.put("error", problem);
                }
                JSONArray array = new JSONArray();
                for (String path : paths) {
                    array.put(path);
                }
                out.put("paths", array);
                out.put("cancelled", problem.isEmpty() && paths.isEmpty());
            } catch (Exception ignored) {
                return "{\"error\":\"the picker could not report what was chosen\"}";
            }
            return out.toString();
        }
    }
}
