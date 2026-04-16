# Quick test
driver = create_driver()
authenticate(driver)

# Try getting total pages
total = get_total_page_count(driver)
print(f"Total pages: {total}")

# Collect just first 3 pages
links = []
for i in range(3):
    new_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='SelectedIDforPrint']")
    for l in new_links:
        href = l.get_attribute("href")
        if href and href not in links:
            links.append(href)
    print(f"Page {i+1}: {len(links)} total links")
    click_next_page(driver)
    time.sleep(2)

print(f"\nSample URLs: {links[:3]}")
driver.quit()