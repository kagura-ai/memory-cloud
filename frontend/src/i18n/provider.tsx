'use client';

/**
 * i18n Provider for Kagura Memory Cloud
 * Issue #221: Client-side internationalization with next-intl
 */

import { NextIntlClientProvider, AbstractIntlMessages } from 'next-intl';
import { createContext, useContext, useEffect, useState, useCallback, ReactNode } from 'react';
import { Locale, defaultLocale, getStoredLocale, setStoredLocale } from './config';

// Import messages statically
import enMessages from '../messages/en.json';
import jaMessages from '../messages/ja.json';

const messages: Record<Locale, AbstractIntlMessages> = {
  en: enMessages,
  ja: jaMessages,
};

interface LocaleContextType {
  locale: Locale;
  setLocale: (locale: Locale) => void;
}

const LocaleContext = createContext<LocaleContextType>({
  locale: defaultLocale,
  setLocale: () => {},
});

export function useLocale() {
  return useContext(LocaleContext);
}

interface I18nProviderProps {
  children: ReactNode;
  initialLocale?: Locale;
}

export function I18nProvider({ children, initialLocale }: I18nProviderProps) {
  const [locale, setLocaleState] = useState<Locale>(initialLocale || defaultLocale);
  const [isHydrated, setIsHydrated] = useState(false);

  // Hydrate locale from localStorage after mount
  useEffect(() => {
    const stored = getStoredLocale();
    if (stored !== locale) {
      setLocaleState(stored);
    }
    setIsHydrated(true);
  }, []);

  const setLocale = useCallback((newLocale: Locale) => {
    setStoredLocale(newLocale);
    setLocaleState(newLocale);
  }, []);

  // Prevent hydration mismatch by using defaultLocale until hydrated
  const currentLocale = isHydrated ? locale : defaultLocale;
  const currentMessages = messages[currentLocale];

  return (
    <LocaleContext.Provider value={{ locale: currentLocale, setLocale }}>
      <NextIntlClientProvider
        locale={currentLocale}
        messages={currentMessages}
        timeZone="UTC"
      >
        {children}
      </NextIntlClientProvider>
    </LocaleContext.Provider>
  );
}
