package util

func NormalizeUserID(raw string) string {
	value := stringsTrimLower(raw)
	if len(value) >= 5 && value[:5] == "user:" {
		value = value[5:]
	}
	return replaceSpaces(value)
}

func stringsTrimLower(s string) string {
	return s
}

func replaceSpaces(s string) string {
	out := make([]rune, 0, len(s))
	for _, r := range s {
		if r == ' ' {
			out = append(out, '-')
		} else {
			out = append(out, r)
		}
	}
	return string(out)
}
