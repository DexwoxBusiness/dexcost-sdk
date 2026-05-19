package main

import (
	"bytes"
	"context"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/chromedp"
)

// Lower bound for what counts as a real authenticated dashboard render.
// A 1920x1080 page with sidebar + a chart sits at 100+ KB; an unauthenticated
// /login redirect or a 404 bottoms out around 8–20 KB. 50 KB sits comfortably
// between the two and was the threshold the UI Designer asked for in DEX-276.
const minScreenshotBytes = 50 * 1024

// screenshotDashboard captures full-viewport PNGs of the dashboard, asserting
// for each page that (a) the navigation returned HTTP 200, (b) the
// authenticated layout's <aside> sidebar is in the DOM, and (c) the saved
// PNG is not byte-identical to a previously saved one. apiBase is the
// control-layer (e.g. http://localhost:3000), dashboardBase is the Next.js
// dashboard (e.g. http://localhost:3001) — these are different services on
// different ports, conflating them is the original DEX-276 regression.
func screenshotDashboard(ctx context.Context, dashboardBase, apiBase, outDir string) error {
	email := os.Getenv("DEXCOST_DASHBOARD_EMAIL")
	if email == "" {
		return fmt.Errorf("DEXCOST_DASHBOARD_EMAIL not set")
	}
	password := os.Getenv("DEXCOST_DASHBOARD_PASSWORD")
	if password == "" {
		return fmt.Errorf("DEXCOST_DASHBOARD_PASSWORD not set")
	}

	sessionToken, err := dashboardLogin(ctx, apiBase, email, password)
	if err != nil {
		return fmt.Errorf("dashboard login: %w", err)
	}
	slog.Info("dashboard login succeeded", "api", apiBase, "email", email)

	cookieDomain, err := hostOf(dashboardBase)
	if err != nil {
		return err
	}

	pages := []struct {
		path string
		name string
	}{
		{"/dashboard", "overview"},
		{"/dashboard/cost-health", "cost-health"},
		{"/dashboard/tasks", "tasks"},
	}

	var firstErr error
	for _, p := range pages {
		pageURL := strings.TrimRight(dashboardBase, "/") + p.path
		outPath := filepath.Join(outDir, fmt.Sprintf("dashboard-%s.png", p.name))

		slog.Info("screenshot", "page", p.name, "url", pageURL)
		if err := capturePage(ctx, pageURL, cookieDomain, sessionToken, outPath); err != nil {
			slog.Warn("screenshot failed", "page", p.name, "error", err)
			if firstErr == nil {
				firstErr = err
			}
			time.Sleep(2 * time.Second)
			continue
		}
		time.Sleep(2 * time.Second)
	}

	if firstErr != nil {
		return firstErr
	}
	return verifyScreenshots(outDir, len(pages))
}

// dashboardLogin authenticates against the control-layer's session endpoint
// and returns the session-cookie value. Returning the raw token (rather than
// a *http.Cookie) keeps the chromedp side decoupled from net/http types.
func dashboardLogin(ctx context.Context, apiBase, email, password string) (string, error) {
	body, err := json.Marshal(map[string]interface{}{
		"email":       email,
		"password":    password,
		"remember_me": false,
	})
	if err != nil {
		return "", fmt.Errorf("marshal login body: %w", err)
	}

	loginURL := strings.TrimRight(apiBase, "/") + "/v1/auth/login"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, loginURL, bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("build login request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(req)
	if err != nil {
		return "", fmt.Errorf("post login: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		preview, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return "", fmt.Errorf("login non-200: status=%d body=%q", resp.StatusCode, string(preview))
	}

	for _, c := range resp.Cookies() {
		if c.Name == "session" && c.Value != "" {
			return c.Value, nil
		}
	}
	return "", fmt.Errorf("login response had no 'session' cookie (Set-Cookie missing)")
}

// capturePage runs an isolated headless browser, injects the session cookie
// before the navigation request fires, asserts the main-frame document
// returned HTTP 200, and waits for the dashboard's persistent sidebar to be
// visible — only THEN is a screenshot saved. Each guard is a separate failure
// mode that previously got silently swallowed (DEX-276).
func capturePage(ctx context.Context, pageURL, cookieDomain, sessionToken, outPath string) error {
	allocCtx, cancelAlloc := chromedp.NewExecAllocator(ctx,
		chromedp.ExecPath("/usr/bin/chromium"),
		chromedp.Headless,
		chromedp.NoSandbox,
		chromedp.DisableGPU,
		chromedp.WindowSize(1920, 1080),
	)
	defer cancelAlloc()

	browserCtx, cancelBrowser := chromedp.NewContext(allocCtx)
	defer cancelBrowser()

	timeoutCtx, cancelTimeout := context.WithTimeout(browserCtx, 45*time.Second)
	defer cancelTimeout()

	target, err := url.Parse(pageURL)
	if err != nil {
		return fmt.Errorf("parse page URL: %w", err)
	}

	// Capture the first main-frame response. Subresources (CSS, JS, /api/*)
	// also fire EventResponseReceived but we only care about the document.
	var (
		mu         sync.Mutex
		mainStatus int64
		mainURL    string
	)
	chromedp.ListenTarget(timeoutCtx, func(ev interface{}) {
		resp, ok := ev.(*network.EventResponseReceived)
		if !ok {
			return
		}
		if resp.Type != network.ResourceTypeDocument {
			return
		}
		mu.Lock()
		defer mu.Unlock()
		if mainStatus == 0 {
			mainStatus = resp.Response.Status
			mainURL = resp.Response.URL
		}
	})

	expr := cdp.TimeSinceEpoch(time.Now().Add(24 * time.Hour))

	var buf []byte
	runErr := chromedp.Run(timeoutCtx,
		network.Enable(),
		chromedp.ActionFunc(func(ctx context.Context) error {
			return network.SetCookie("session", sessionToken).
				WithDomain(cookieDomain).
				WithPath("/").
				WithExpires(&expr).
				WithHTTPOnly(true).
				WithSameSite(network.CookieSameSiteLax).
				Do(ctx)
		}),
		chromedp.Navigate(pageURL),
		// The auth-required layout's <aside> sidebar renders on every dashboard
		// route. /login and a 404 don't have one, so this WaitVisible is what
		// actually catches a redirect — chromedp.Navigate happily returns nil
		// even when we land on the wrong page.
		chromedp.WaitVisible(`aside`, chromedp.ByQuery),
		chromedp.Sleep(2 * time.Second),
		chromedp.FullScreenshot(&buf, 90),
	)

	mu.Lock()
	status, finalURL := mainStatus, mainURL
	mu.Unlock()

	if runErr != nil {
		if status != 0 {
			return fmt.Errorf("chromedp run (status=%d, final=%s): %w", status, finalURL, runErr)
		}
		return fmt.Errorf("chromedp run: %w", runErr)
	}
	if status != 0 && status != http.StatusOK {
		return fmt.Errorf("non-200 main-frame status: got %d for %s (final URL: %s)", status, pageURL, finalURL)
	}
	if finalURL != "" {
		gotPath := strings.TrimRight(pathOf(finalURL), "/")
		wantPath := strings.TrimRight(target.Path, "/")
		if gotPath != wantPath {
			return fmt.Errorf("redirected away from %s to %s — auth probably did not stick", pageURL, finalURL)
		}
	}
	if len(buf) < minScreenshotBytes {
		return fmt.Errorf("screenshot too small: %d bytes (< %d minimum)", len(buf), minScreenshotBytes)
	}

	if err := os.WriteFile(outPath, buf, 0644); err != nil {
		return fmt.Errorf("write screenshot: %w", err)
	}
	slog.Info("screenshot saved", "path", outPath, "bytes", len(buf), "status", status)
	return nil
}

// verifyScreenshots is the safety net that catches the DEX-276 regression: a
// run that wrote N PNGs that all happen to be byte-identical (every page
// rendered the same /login or /404). We reject undersized files and any set
// where two captures share an MD5.
func verifyScreenshots(outDir string, expected int) error {
	matches, err := filepath.Glob(filepath.Join(outDir, "dashboard-*.png"))
	if err != nil {
		return fmt.Errorf("glob screenshots: %w", err)
	}
	if len(matches) < expected {
		return fmt.Errorf("expected %d screenshots, found %d", expected, len(matches))
	}

	seen := make(map[string]string, len(matches))
	for _, p := range matches {
		data, err := os.ReadFile(p)
		if err != nil {
			return fmt.Errorf("read %s: %w", p, err)
		}
		if len(data) < minScreenshotBytes {
			return fmt.Errorf("%s is %d bytes, below %d minimum", p, len(data), minScreenshotBytes)
		}
		sum := md5.Sum(data)
		hash := hex.EncodeToString(sum[:])
		if other, ok := seen[hash]; ok {
			return fmt.Errorf("duplicate screenshot content: %s and %s share md5 %s — both likely captured the same error or redirect page",
				filepath.Base(p), filepath.Base(other), hash)
		}
		seen[hash] = p
	}
	slog.Info("screenshot verification passed", "count", len(matches))
	return nil
}

func hostOf(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("parse URL %q: %w", raw, err)
	}
	if u.Hostname() == "" {
		return "", fmt.Errorf("URL %q has no host", raw)
	}
	return u.Hostname(), nil
}

func pathOf(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return u.Path
}
