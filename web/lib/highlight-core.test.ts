/**
 * Tests for search highlight parsing (Blueprint §12).
 *
 * Run with: npm test  (node --test, no extra dependencies)
 *
 * The security-relevant property is that `segments()` never returns markup:
 * whatever the backend sends, every segment must be plain text that React can
 * escape. The XSS cases below are the ones that actually reach production —
 * tender titles are scraped from third-party government sites.
 */
import assert from "node:assert/strict";
import { test, describe } from "node:test";

import { decodeEntities, hasMatch, plainText, segments } from "./highlight-core.ts";

describe("segments", () => {
  test("splits a marked term from surrounding text", () => {
    assert.deepEqual(segments("<mark>Construction</mark> of a clinic"), [
      { marked: true, text: "Construction" },
      { marked: false, text: " of a clinic" },
    ]);
  });

  test("handles a match in the middle", () => {
    assert.deepEqual(segments("Bou van <mark>kliniek</mark> nou"), [
      { marked: false, text: "Bou van " },
      { marked: true, text: "kliniek" },
      { marked: false, text: " nou" },
    ]);
  });

  test("handles multiple and adjacent matches", () => {
    assert.deepEqual(segments("<mark>a</mark><mark>b</mark>"), [
      { marked: true, text: "a" },
      { marked: true, text: "b" },
    ]);
  });

  test("returns one unmarked segment when nothing matched", () => {
    assert.deepEqual(segments("no matches here"), [
      { marked: false, text: "no matches here" },
    ]);
  });

  test("is empty for empty input", () => {
    assert.deepEqual(segments(""), []);
    assert.deepEqual(segments(null), []);
    assert.deepEqual(segments(undefined), []);
  });

  test("reassembles to the original plain text", () => {
    const input = "Dept: <mark>Sanitation</mark> for schools &amp; clinics";
    const joined = segments(input).map((s) => s.text).join("");
    assert.equal(joined, "Dept: Sanitation for schools & clinics");
  });

  test("is stateless across calls (regex lastIndex reset)", () => {
    const input = "<mark>x</mark> y";
    assert.deepEqual(segments(input), segments(input));
  });
});

describe("segments — XSS", () => {
  test("escaped script tags come back as inert text, not markup", () => {
    const parts = segments("&lt;script&gt;alert(1)&lt;/script&gt; <mark>bou</mark>");
    assert.deepEqual(parts[0], {
      marked: false,
      text: "<script>alert(1)</script> ",
    });
    // The angle brackets are DATA in a text node: React will escape them.
    assert.equal(parts[1].marked, true);
  });

  test("an unescaped tag is never treated as markup", () => {
    // If backend escaping ever regresses, we must still hand React text.
    const parts = segments("<img src=x onerror=alert(1)> <mark>hit</mark>");
    const text = parts.map((s) => s.text).join("");
    assert.ok(text.includes("<img src=x onerror=alert(1)>"));
    assert.equal(parts.filter((s) => s.marked).length, 1);
  });

  test("a mark tag with attributes is not our delimiter", () => {
    const parts = segments('<mark onmouseover=alert(1)>x</mark>');
    assert.equal(parts.length, 1);
    assert.equal(parts[0].marked, false);
  });

  test("unbalanced tags do not throw or hang", () => {
    for (const input of ["<mark>open", "</mark>", "<mark>", "<mark><mark>a"]) {
      assert.doesNotThrow(() => segments(input));
    }
  });
});

describe("decodeEntities", () => {
  test("decodes what the backend escapes", () => {
    assert.equal(decodeEntities("R&amp;D"), "R&D");
    assert.equal(decodeEntities("&lt;a&gt;"), "<a>");
    assert.equal(decodeEntities("Bou van &#39;n huis"), "Bou van 'n huis");
    assert.equal(decodeEntities("&quot;quoted&quot;"), '"quoted"');
  });

  test("leaves unknown entities alone", () => {
    assert.equal(decodeEntities("&nbsp;&copy;"), "&nbsp;&copy;");
  });

  test("does not double-decode", () => {
    // "&amp;lt;" is a literal "&lt;" and must survive as one.
    assert.equal(decodeEntities("&amp;lt;"), "&lt;");
  });
});

describe("plainText", () => {
  test("strips marks and decodes", () => {
    assert.equal(
      plainText("<mark>SUPPLY</mark> of R&amp;D services"),
      "SUPPLY of R&D services",
    );
  });

  test("is empty for nullish input", () => {
    assert.equal(plainText(null), "");
    assert.equal(plainText(undefined), "");
  });
});

describe("hasMatch", () => {
  test("true only when something was marked", () => {
    assert.equal(hasMatch("<mark>x</mark>"), true);
    assert.equal(hasMatch("plain"), false);
    assert.equal(hasMatch(null), false);
    assert.equal(hasMatch(""), false);
  });
});
