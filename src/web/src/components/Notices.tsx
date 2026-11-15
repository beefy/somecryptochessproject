/**
 * One notification bar for the whole app.
 *
 * A Paper `Snackbar` positions itself absolutely inside its nearest positioned
 * ancestor, so one rendered by a view inside the page scroller sat at the bottom
 * of the *content* rather than the bottom of the window: it floated mid-page and
 * moved with the scroll. Mounting it once, outside the scroll view, is what puts
 * every notice where a notice belongs.
 *
 * Views call `useNotice()`; the message is not keyed to a view, so an "included"
 * notice can outlive the tab that started the transfer.
 */

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { StyleSheet } from "react-native";
import { Snackbar } from "react-native-paper";

/** Raise a message; the next call replaces whatever is showing. */
export type Notify = (message: string) => void;

const NoticeContext = createContext<Notify>(() => undefined);

export function NoticeProvider({ children }: { children: ReactNode }): ReactNode {
  const [message, setMessage] = useState<string | null>(null);
  // Remounting restarts the snackbar's timer and entrance animation, so a second
  // message is not queued behind the first one's remaining duration.
  const [revision, setRevision] = useState(0);

  const notify = useCallback<Notify>((text) => {
    setMessage(text);
    setRevision((value) => value + 1);
  }, []);

  return (
    <NoticeContext.Provider value={notify}>
      {children}
      <Snackbar
        key={revision}
        visible={message !== null}
        onDismiss={() => setMessage(null)}
        duration={6000}
        action={{ label: "Dismiss", onPress: () => setMessage(null) }}
        wrapperStyle={styles.wrapper}
        style={styles.snackbar}
      >
        {message ?? ""}
      </Snackbar>
    </NoticeContext.Provider>
  );
}

/** The app-wide notification bar. */
export function useNotice(): Notify {
  return useContext(NoticeContext);
}

const styles = StyleSheet.create({
  wrapper: { alignItems: "center" },
  snackbar: { width: "100%", maxWidth: 720 },
});
