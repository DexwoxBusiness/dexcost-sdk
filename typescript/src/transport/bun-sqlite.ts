/**
 * bun:sqlite compatibility layer — durable buffering on Bun.
 *
 * better-sqlite3's native binding is unsupported on Bun (bun#4290), which
 * previously forced Bun deployments onto the in-memory fallback (events
 * lost on restart). Bun ships a built-in SQLite driver (`bun:sqlite`)
 * whose Statement API (`prepare().run/get/all`) is positionally
 * compatible with better-sqlite3's; this adapter papers over the two
 * differences the EventBuffer relies on:
 *
 * - `pragma(directive)` — bun:sqlite has no pragma() method; shimmed via
 *   a prepared `PRAGMA ...` statement.
 * - construction options — bun:sqlite wants `{ create: true }` to create
 *   a missing database file.
 *
 * Loaded lazily and ONLY on Bun; returns null anywhere else so the
 * regular better-sqlite3 path is untouched.
 */

import { createRequire } from "node:module";
import { isBun } from "../core/runtime.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

const _require = createRequire(import.meta.url);

/**
 * Returns a better-sqlite3-shaped Database constructor backed by
 * bun:sqlite, or null when not on Bun / bun:sqlite unavailable.
 */
export function loadBunSqliteCompat(): (new (path: string) => any) | null {
  if (!isBun()) return null;

  let BunDatabase: any;
  try {
    BunDatabase = _require("bun:sqlite").Database;
  } catch {
    return null;
  }
  if (typeof BunDatabase !== "function") return null;

  class BunSqliteCompat {
    private _db: any;

    constructor(path: string) {
      this._db = new BunDatabase(path, { create: true });
    }

    /** Statement API is positionally compatible (run/get/all with ? params). */
    prepare(sql: string): any {
      return this._db.prepare(sql);
    }

    exec(sql: string): void {
      this._db.exec(sql);
    }

    /** better-sqlite3-style pragma("journal_mode=WAL") / pragma("wal_checkpoint(TRUNCATE)"). */
    pragma(directive: string): unknown {
      return this._db.prepare(`PRAGMA ${directive}`).all();
    }

    close(): void {
      this._db.close();
    }
  }

  return BunSqliteCompat;
}
