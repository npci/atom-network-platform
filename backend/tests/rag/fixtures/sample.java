package com.example.payment;

public class RateLimiter {
    private int limit;
    private int count;

    public RateLimiter(int limit) {
        this.limit = limit;
        this.count = 0;
    }

    public boolean acquire() {
        this.count += 1;
        return this.count <= this.limit;
    }

    public int remaining() {
        return this.limit - this.count;
    }
}
