import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  SourceAnchorError,
  verifyTextAnchor,
} from "../src/features/archive/source-anchor.ts";

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

test("text anchors use Unicode code points rather than UTF-16 units", async () => {
  const original = "Before 🧠 revenue grew.\nAfter";
  const exactText = "🧠 revenue";
  const startCharacter = Array.from(original).indexOf("🧠");
  const endCharacter = startCharacter + Array.from(exactText).length;

  const result = await verifyTextAnchor({
    original,
    startCharacter,
    endCharacter,
    exactText,
    textSha256: sha256(exactText),
  });

  assert.deepEqual(result, {
    before: "Before ",
    exact: exactText,
    after: " grew.\nAfter",
  });
});

test("text anchors use the stored range when exact text is repeated", async () => {
  const original = "same | same";

  const result = await verifyTextAnchor({
    original,
    startCharacter: 7,
    endCharacter: 11,
    exactText: "same",
    textSha256: sha256("same"),
  });

  assert.equal(result.before, "same | ");
  assert.equal(result.exact, "same");
  assert.equal(result.after, "");
});

test("text anchors fail closed for invalid ranges, text, and checksums", async () => {
  const valid = {
    original: "verified text",
    startCharacter: 0,
    endCharacter: 8,
    exactText: "verified",
    textSha256: sha256("verified"),
  };

  await assert.rejects(
    verifyTextAnchor({ ...valid, endCharacter: 80 }),
    SourceAnchorError,
  );
  await assert.rejects(
    verifyTextAnchor({ ...valid, exactText: "changed!" }),
    SourceAnchorError,
  );
  await assert.rejects(
    verifyTextAnchor({ ...valid, textSha256: "0".repeat(64) }),
    SourceAnchorError,
  );
});
