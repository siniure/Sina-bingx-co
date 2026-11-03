package com.sina.oco;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.view.View;
import android.webkit.SslErrorHandler;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.net.URI;

/** HTTPS viewer only; no exchange or bot credentials in the application. */
public class MainActivity extends Activity {
    private WebView web;
    private String allowedHost = "";
    private int allowedPort = -1;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        showSetup();
    }

    private void showSetup() {
        if (web != null) { web.destroy(); web = null; }
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int space = (int)(22 * getResources().getDisplayMetrics().density);
        layout.setPadding(space, space, space, space);
        layout.setBackgroundColor(Color.rgb(14, 23, 28));
        layout.setOnApplyWindowInsetsListener((v, insets) -> {
            v.setPadding(space, space + insets.getSystemWindowInsetTop(), space, space + insets.getSystemWindowInsetBottom());
            return insets;
        });
        TextView title = new TextView(this);
        title.setText("SINA OCO\nاتصال به سرور خودت");
        title.setTextSize(24); title.setTextColor(Color.rgb(199,236,131));
        EditText address = new EditText(this);
        address.setSingleLine(true);
        address.setTextDirection(View.TEXT_DIRECTION_LTR);
        address.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        address.setHint("https://…");
        address.setText(getPreferences(MODE_PRIVATE).getString("server_url", ""));
        TextView status = new TextView(this);
        status.setText("انتشار زنده غیرفعال است. این برنامه وضعیت سرور را نشان می‌دهد؛ رمز صرافی لازم نیست.");
        status.setTextColor(Color.LTGRAY);
        Button open = new Button(this); open.setText("بازکردن سرور امن");
        open.setOnClickListener(v -> {
            try {
                URI uri = new URI(address.getText().toString().trim());
                if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null
                        || uri.getQuery() != null || uri.getFragment() != null ||
                        (uri.getPath() != null && !uri.getPath().isEmpty() && !"/".equals(uri.getPath()))) throw new Exception();
                allowedHost = uri.getHost(); allowedPort = uri.getPort();
                String normalized = uri.toString();
                getPreferences(MODE_PRIVATE).edit().putString("server_url", normalized).apply();
                showWeb(normalized);
            } catch (Exception e) { status.setText("نشانی HTTPS ریشهٔ سرور را وارد کن؛ کلید و رمز را داخل نشانی نگذار."); }
        });
        layout.addView(title); layout.addView(address); layout.addView(open); layout.addView(status);
        setContentView(layout);
    }

    private void showWeb(String address) {
        web = new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(false);
        web.getSettings().setAllowFileAccess(false);
        web.getSettings().setAllowContentAccess(false);
        web.getSettings().setMixedContentMode(android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        web.getSettings().setSafeBrowsingEnabled(true);
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (!"https".equalsIgnoreCase(uri.getScheme())) return true;
                if (allowedHost.equalsIgnoreCase(uri.getHost()) && allowedPort == uri.getPort()) return false;
                if ("t.me".equalsIgnoreCase(uri.getHost())) {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); } catch (Exception ignored) { }
                }
                return true;
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) { handler.cancel(); }
        });
        setContentView(web); web.loadUrl(address);
    }

    @Override public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else if (web != null) showSetup();
        else super.onBackPressed();
    }
    @Override protected void onDestroy() { if (web != null) web.destroy(); super.onDestroy(); }
}
