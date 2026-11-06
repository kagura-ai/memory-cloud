"use client";

import { useState, type ComponentProps } from "react";
import { useTranslations } from "next-intl";
import { Input } from "@/components/ui/input";

type Props = ComponentProps<"input"> & { allowRecovery?: boolean };

export function MfaCodeInput({ allowRecovery = false, placeholder, ...props }: Props) {
  const t = useTranslations("mfaSettings");
  const [recoveryKeyboard, setRecoveryKeyboard] = useState(false);
  return (
    <>
      <Input
        {...props}
        type="text"
        inputMode={allowRecovery && recoveryKeyboard ? "text" : "numeric"}
        autoComplete="one-time-code"
        autoCapitalize="none"
        spellCheck={false}
        placeholder={allowRecovery && recoveryKeyboard ? undefined : placeholder}
      />
      {allowRecovery && (
        <button type="button" className="text-sm underline underline-offset-2"
          aria-controls={props.id} onClick={() => setRecoveryKeyboard(!recoveryKeyboard)}>
          {t(recoveryKeyboard ? "useAuthenticatorCode" : "useRecoveryCode")}
        </button>
      )}
    </>
  );
}
