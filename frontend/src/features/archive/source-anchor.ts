export interface VerifiedTextAnchor {
  readonly before: string;
  readonly exact: string;
  readonly after: string;
}

export class SourceAnchorError extends Error {
  constructor() {
    super("The source anchor could not be verified.");
    this.name = "SourceAnchorError";
  }
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export async function verifyTextAnchor(input: {
  readonly original: string;
  readonly startCharacter: number;
  readonly endCharacter: number;
  readonly exactText: string;
  readonly textSha256: string;
}): Promise<VerifiedTextAnchor> {
  const { original, startCharacter, endCharacter, exactText, textSha256 } =
    input;
  const codePoints = Array.from(original);

  if (
    !Number.isSafeInteger(startCharacter) ||
    !Number.isSafeInteger(endCharacter) ||
    startCharacter < 0 ||
    endCharacter <= startCharacter ||
    endCharacter > codePoints.length
  ) {
    throw new SourceAnchorError();
  }

  const exact = codePoints.slice(startCharacter, endCharacter).join("");
  if (exact !== exactText || (await sha256(exact)) !== textSha256) {
    throw new SourceAnchorError();
  }

  return {
    before: codePoints.slice(0, startCharacter).join(""),
    exact,
    after: codePoints.slice(endCharacter).join(""),
  };
}
