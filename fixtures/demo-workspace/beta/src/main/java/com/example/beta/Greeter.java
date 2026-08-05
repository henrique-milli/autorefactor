package com.example.beta;

public class Greeter {
    public String greet(String name) {
        if (name == null || name.isBlank()) {
            return "hello";
        }
        return "hello " + name.trim().toLowerCase();
    }
}
