package alpha;

import java.util.List;

public class Billing {
    public int computeTotal(List<Order> orders) {
        int sum = 0;
        for (Order order : orders) {
            if (order != null && order.isActive()) {
                sum += order.getAmount();
                sum += order.getTax();
                if (order.hasDiscount()) {
                    sum -= order.getDiscount();
                }
            }
        }
        return Math.max(sum, 0);
    }
}
