import { fireEvent, screen } from "@testing-library/react";
import { LanguageSwitcher } from "@/components/layout/language-switcher";
import { renderWithProviders } from "../support/render";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }) }));

describe("LanguageSwitcher", () => {
  beforeEach(() => {
    refresh.mockClear();
    document.cookie = "ragops-locale=; max-age=0; path=/";
  });

  it("marks the active language and is reachable by name", () => {
    renderWithProviders(<LanguageSwitcher />);
    expect(screen.getByRole("group", { name: "Sprache der Benutzeroberfläche" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deutsch (Deutschland)" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Englisch (USA)" })).toHaveAttribute("aria-pressed", "false");
  });

  it("stores only the preference cookie and refreshes in place", () => {
    renderWithProviders(<LanguageSwitcher />);
    fireEvent.click(screen.getByTestId("locale-en-US"));
    expect(document.cookie).toContain("ragops-locale=en-US");
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does nothing when the active language is chosen again", () => {
    renderWithProviders(<LanguageSwitcher />, { locale: "en-US" });
    expect(screen.getByRole("button", { name: "English (United States)" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("locale-en-US"));
    expect(refresh).not.toHaveBeenCalled();
  });
});
