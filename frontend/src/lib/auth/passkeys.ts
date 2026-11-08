import { apiClient } from "@/lib/api/base";

export const passkeyPath = "/api/v1/auth/passkeys";
export type Passkey = {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
};

/** The account's passkeys with the server's per-account limit (#2713). */
export type PasskeyList = { keys: Passkey[]; limit: number };

export function listPasskeys(): Promise<PasskeyList> {
  return apiClient.get<PasskeyList>(`${passkeyPath}?include_limit=true`);
}

type Descriptor = {
  type: "public-key";
  id: string;
  transports?: AuthenticatorTransport[];
};
type RequestOptions = Omit<
  PublicKeyCredentialRequestOptions,
  "challenge" | "allowCredentials"
> & {
  challenge: string;
  allowCredentials?: Descriptor[];
};
type CreationOptions = Omit<
  PublicKeyCredentialCreationOptions,
  "challenge" | "user" | "excludeCredentials"
> & {
  challenge: string;
  user: { id: string; name: string; displayName: string };
  excludeCredentials?: Descriptor[];
};

function decode(value: string): ArrayBuffer {
  return Uint8Array.from(
    atob(value.replace(/-/g, "+").replace(/_/g, "/")),
    (c) => c.charCodeAt(0),
  ).buffer;
}
function encode(value: ArrayBuffer): string {
  return btoa(
    Array.from(new Uint8Array(value), (byte) => String.fromCharCode(byte)).join(
      "",
    ),
  )
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}
function descriptor(item: Descriptor): PublicKeyCredentialDescriptor {
  return { ...item, id: decode(item.id) };
}

async function getAssertion(
  options: RequestOptions,
  signal?: AbortSignal,
  conditional = false,
) {
  const result = (await navigator.credentials.get({
    publicKey: {
      ...options,
      challenge: decode(options.challenge),
      allowCredentials: options.allowCredentials?.map(descriptor),
    },
    mediation: conditional ? "conditional" : "optional",
    signal,
  })) as PublicKeyCredential | null;
  if (!result) throw new Error("Passkey unavailable");
  const response = result.response as AuthenticatorAssertionResponse;
  return {
    id: result.id,
    rawId: encode(result.rawId),
    type: result.type,
    response: {
      authenticatorData: encode(response.authenticatorData),
      clientDataJSON: encode(response.clientDataJSON),
      signature: encode(response.signature),
      userHandle: response.userHandle ? encode(response.userHandle) : null,
    },
  };
}

export async function signInWithPasskey(
  returnTo: string,
  acceptedTerms: string | undefined,
  signal: AbortSignal,
  conditional = false,
) {
  const options = await apiClient.post<RequestOptions>(
    passkeyPath + "/authenticate/options",
    {},
    { signal },
  );
  const credential = await getAssertion(options, signal, conditional);
  const result = await apiClient.post<{ redirect_url: string }>(
    passkeyPath +
      "/authenticate/verify?return_to=" +
      encodeURIComponent(returnTo),
    { challenge: options.challenge, credential, accepted_terms: acceptedTerms },
    { signal },
  );
  return result.redirect_url;
}

export async function managementProof(
  password: string,
  otherPasskey: boolean,
  removing?: string,
  linking?: "google" | "github",
) {
  if (password) return { current_password: password };
  if (!otherPasskey) return {};
  // One purpose per challenge: removing a key, or connecting a provider.
  const query = removing
    ? "?removing=" + encodeURIComponent(removing)
    : linking
      ? "?linking=" + encodeURIComponent(linking)
      : "";
  const options = await apiClient.post<RequestOptions>(
    passkeyPath + "/proof/options" + query,
    {},
  );
  return {
    challenge: options.challenge,
    assertion: await getAssertion(options),
  };
}

export async function registerPasskey(
  proof: Awaited<ReturnType<typeof managementProof>>,
) {
  const options = await apiClient.post<CreationOptions>(
    passkeyPath + "/register/options",
    proof,
  );
  const result = (await navigator.credentials.create({
    publicKey: {
      ...options,
      challenge: decode(options.challenge),
      user: { ...options.user, id: decode(options.user.id) },
      excludeCredentials: options.excludeCredentials?.map(descriptor),
    },
  })) as PublicKeyCredential | null;
  if (!result) throw new Error("Passkey unavailable");
  const response = result.response as AuthenticatorAttestationResponse;
  return apiClient.post<Passkey>(passkeyPath + "/register/verify", {
    challenge: options.challenge,
    credential: {
      id: result.id,
      rawId: encode(result.rawId),
      type: result.type,
      response: {
        attestationObject: encode(response.attestationObject),
        clientDataJSON: encode(response.clientDataJSON),
        transports: response.getTransports?.() ?? [],
      },
    },
  });
}
