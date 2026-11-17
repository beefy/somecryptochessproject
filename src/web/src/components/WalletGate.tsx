/**
 * The unlock boundary for Play, DEX and Bridge.
 *
 * Those pages sign ledger transactions with the wallet's hybrid key, so the key
 * has to be unlocked first. The gate reuses the wallet's own setup screen rather
 * than a second password prompt, which means create, restore, open-keyfile and
 * unlock all behave exactly as they do on the wallet page.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { HelperText } from "react-native-paper";

import { Loading, Section } from "./Chrome";
import { useWalletSession } from "./WalletSession";
import { WalletSetup } from "../views/wallet/Setup";
import { useNode } from "../node";

export function WalletGate({
  purpose,
  children,
}: {
  purpose: string;
  children: ReactNode;
}): ReactNode {
  const { status } = useNode();
  const {
    session,
    stored,
    storeError,
    selected,
    select,
    unlock,
    save,
    forget,
  } = useWalletSession();

  if (session !== null) return children;
  if (stored === null && storeError === null) return <Loading />;

  return (
    <View style={styles.root}>
      <Section
        title={`Unlock the wallet for ${purpose}`}
        subtitle="the same keyfile and passphrase the wallet uses"
      >
        <HelperText type="info" visible>
          {purpose} signs ordinary SCCP transactions from the account in this browser. Nothing is
          sent until a page shows what it will sign; locking from the header clears the key
          immediately.
        </HelperText>
      </Section>
      <WalletSetup
        network={status.data?.network ?? null}
        stored={stored ?? []}
        storeError={storeError}
        selected={selected}
        onSelect={select}
        onUnlock={unlock}
        onImport={save}
        onCreated={save}
        onForget={forget}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { gap: 16 },
});
